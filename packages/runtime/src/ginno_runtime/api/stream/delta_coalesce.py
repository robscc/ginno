"""token.delta / thinking.delta 合帧器（2026-11-11，并发改造集第一项）。

现状：每个 LLM chunk 一帧 WS——N 路并发 turn 时 json.dumps + send_text +
前端 setState 全链路放大（GIL 下 CPU 串行段集体变慢的主因之一）。

方案：per-turn 缓冲 + 33ms 窗口（≈30fps，人眼无感）定时 flush；同 kind
连续 delta 拼接成一帧。协议不变（前端仍收 token.delta {content}，只是
content 更长），handle.ts 零改动。

正确性约束（测试缝见 test_delta_coalesce.py）：
- **事件顺序**：非 delta 事件发射前必须 flush——tool.start 不能跑到它
  前面的文本前面去；turn 收尾（message.end / finally）强制 flush。
- **首 token 不延迟**：缓冲为空时第一片立即发（打字机首字不卡）。
- **有状态过滤器不合帧**：attach_filter / narration gate 在 feed 之前
  逐 fragment 处理（engine 侧接线保证），合帧器只拼纯文本。
- 窗口可配（settings.stream.coalesce_ms，0=关闭回退现状）。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

# 默认 33ms ≈ 30fps。用户拍板值（2026-11-11）。
DEFAULT_WINDOW_MS = 33

_DELTA_KINDS = frozenset({"token.delta", "thinking.delta"})


def coalesce_window_s() -> float:
    """读 settings.stream.coalesce_ms；缺失/坏值用默认，0=禁用。"""
    try:
        import json

        from ..paths import settings_path

        p = settings_path()
        if not p.exists():
            return DEFAULT_WINDOW_MS / 1000.0
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        ms = (data.get("stream") or {}).get("coalesce_ms", DEFAULT_WINDOW_MS)
        ms = float(ms)
        if ms < 0:
            return DEFAULT_WINDOW_MS / 1000.0
        return ms / 1000.0
    except Exception:  # noqa: BLE001 — 配置坏了绝不挡流
        return DEFAULT_WINDOW_MS / 1000.0


class DeltaCoalescer:
    """per-turn 合帧器。send 接收已序列化的完整帧字符串。

    用法（engine 侧）：
        coal = DeltaCoalescer(safe_send, emit)
        ...
        await coal.feed("token.delta", txt)      # delta 走缓冲
        await coal.flush(); await safe_send(emit("tool.start", …))  # 非 delta 先 flush
        ...
        await coal.close()                        # finally：flush + 取消定时器
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        emit: Callable[[str, dict], str],
        window_s: float | None = None,
    ):
        self._send = send
        self._emit = emit
        self._window = coalesce_window_s() if window_s is None else window_s
        self._buf: dict[str, list[str]] = {}
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._window > 0

    async def feed(self, kind: str, text: str) -> None:
        if not text:
            return
        if not self.enabled or self._closed:
            await self._send(self._emit(kind, {"content": text}))
            return
        # 「首片」= 该 kind 尚未开流。注意不能用 `not self._buf[kind]`
        # 判断——flush 后缓冲清空但流已开，后续片仍应进窗口合帧。
        first = kind not in self._buf
        if first:
            # 首片立即发：打字机首字零延迟。发送期间让出事件循环，
            # 保证「首片帧」先于后续缓冲帧落地（顺序语义）。
            await self._send(self._emit(kind, {"content": text}))
            self._buf[kind] = []  # 标记该 kind 已开流（非 None）
            return
        self._buf[kind].append(text)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_after())

    async def _flush_after(self) -> None:
        try:
            await asyncio.sleep(self._window)
            await self.flush()
        except asyncio.CancelledError:
            pass

    async def flush_kind(self, kind: str) -> None:
        parts = self._buf.pop(kind, None)
        if parts:
            await self._send(self._emit(kind, {"content": "".join(parts)}))

    async def flush(self) -> None:
        """清空全部缓冲（非 delta 事件前 / turn 收尾调用）。"""
        for kind in list(self._buf):
            await self.flush_kind(kind)

    async def close(self) -> None:
        """finally 调用：flush 残留 + 取消定时器。幂等。"""
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        await self.flush()
