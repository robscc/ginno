"""WeComTap：把引擎 turn 帧翻译成企业微信智能机器人侧动作。

与钉钉卡片（cards.py）不同，企微智能机器人的流式载体是回调帧自身的
``aibot_respond_msg`` stream 消息：同一 ``stream.id`` 反复发送**全量替换**
content、``finish=true`` 定格——因此节流策略是「攒增量、发全量」，断流
重发天然幂等。占位首帧（5 秒回调时限）由 adapter 在受理消息时先发，
本类只续流。

时限兜底：流式全程上限 10 分钟（protocol.STREAM_SAFE_S=9.5min 安全线），
超时强制 finish；此后残余增量在 turn 结束时改走 ``aibot_send_msg``
主动推送（markdown）收尾。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..base import ChannelTap
from . import protocol as p

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .adapter import WeComChannelAdapter

# 节流：企微单会话限频 ~30 msg/min（流式帧计入），3s 一发留出余量
_FLUSH_INTERVAL_S = 3.0

_STATUS_ICON = {"done": "✅", "running": "⏳", "pending": "🕒",
                "ok": "✅", "error": "❌"}

# 产物上行白名单（同钉钉 tap：未知类型不外发，安全默认）
_SENDABLE = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "docx", "xlsx",
             "pptx", "txt", "csv", "md", "html", "zip", "json", "tsv",
             "log", "py", "js", "ts", "go", "rs"}
_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}


def _widget_to_markdown(data: dict) -> str:
    """widget.emit 的渠道降级渲染（同钉钉 tap 的策略，仅翻译真实数据）。"""
    kind = data.get("kind")
    payload = data.get("data") or {}
    lines: list[str] = []
    if kind == "stat_list":
        if payload.get("title"):
            lines.append(f"**{payload['title']}**")
        for it in payload.get("items") or []:
            icon = _STATUS_ICON.get(str(it.get("status", "")), "•")
            lines.append(f"- {icon} {it.get('label', '')}: {it.get('value', '')}")
    elif kind == "chart":
        if payload.get("title"):
            lines.append(f"**{payload['title']}**")
        rows = payload.get("data") or []
        xk, yk = payload.get("x", "x"), payload.get("y", "y")
        if rows:
            lines.append(f"| {xk} | {yk} |")
            lines.append("|---|---|")
            for r in rows[:30]:
                lines.append(f"| {r.get(xk, '')} | {r.get(yk, '')} |")
        if payload.get("summary"):
            lines.append(str(payload["summary"]))
    else:
        return ""
    return "\n".join(lines)


class WeComTap(ChannelTap):
    """一个 turn 一枚 tap：挂 ``_SESSION_WS`` 收帧，驱动企微流式回复。"""

    @classmethod
    def push_only(cls, adapter: "WeComChannelAdapter", chat_key: str) -> "WeComTap":
        """无回调流的 tap（steer 补投等主动场景）：流生而收尾，全部增量
        走 ``_tail`` → close 时 ``send_markdown`` 主动推送——与 9.5 分钟
        安全线的降级同一条出口。"""
        t = cls(adapter, "", chat_key, "")
        t._stream_done = True
        return t

    def __init__(self, adapter: WeComChannelAdapter, req_id: str,
                 chat_key: str, stream_id: str) -> None:
        self.adapter = adapter
        self.req_id = req_id          # 触发消息的回调 req_id（回复窗口 24h）
        self.chat_key = chat_key      # 主动推送兜底出口的目的地
        self.stream_id = stream_id    # adapter 占位首帧已启用的流 id
        self._buf: list[str] = []
        self._buf_len = 0
        self._full = ""               # 已定格进流的全量正文
        self._tail = ""               # 流被强制收尾后的残余增量
        self._last_flush = float("-inf")  # 首批内容立即流出（占位帧后尽快接上）
        self._t0 = time.monotonic()
        self._stream_done = False     # finish=true 已发（超时强制或终态）
        self._finalized = False
        self._ask_timer: asyncio.TimerHandle | None = None
        self._question_id: str | None = None
        self._options: list = []

    # ---- 流式输出 -----------------------------------------------------------

    async def _send_stream(self, content: str, *, finish: bool = False) -> None:
        await self.adapter.client.respond(
            self.req_id, p.stream_body(self.stream_id, content, finish=finish))

    async def _maybe_flush(self, *, force: bool = False) -> None:
        if not self._buf:
            return
        now = time.monotonic()
        if not (force or now - self._last_flush >= _FLUSH_INTERVAL_S):
            return
        chunk = "".join(self._buf)
        self._buf.clear()
        self._buf_len = 0
        self._last_flush = now
        if self._stream_done:
            # 流已按 10 分钟上限收尾：增量改走主动推送缓冲，close 时统一发
            self._tail += chunk
            return
        self._full += chunk
        if now - self._t0 >= p.STREAM_SAFE_S:
            # 9.5 分钟安全线：强制定格，剩余内容兜底主动推送
            self._stream_done = True
            try:
                await self._send_stream(self._full + "\n\n…（内容较长，剩余部分稍后送达）",
                                        finish=True)
            except Exception:  # noqa: BLE001 — 定格失败由 close 的推送兜底
                pass
            return
        try:
            await self._send_stream(self._full)
        except Exception:  # noqa: BLE001 — 单批失败静默，终态保证可见
            pass

    async def _finalize(self, note: str = "") -> None:
        """终态：flush 残余 + finish 定格 + 超时尾部主动推送。幂等。"""
        if self._finalized:
            return
        self._finalized = True
        self._cancel_ask_timer()
        await self._maybe_flush(force=True)
        body = self._full or "（无输出）"
        if note:
            body = f"{body}\n\n---\n{note}"
        if not self._stream_done:
            try:
                self._stream_done = True
                await self._send_stream(body, finish=True)
            except Exception:  # noqa: BLE001
                self._tail = body  # 定格失败也走推送兜底
        else:
            self._tail += note
        if self._tail.strip():
            try:
                await self.adapter.client.send_markdown(
                    self.chat_key, self._tail if self._tail.strip() else body)
            except Exception:  # noqa: BLE001
                pass

    # ---- ask_user（文本作答路径）---------------------------------------------

    async def _send_question(self, data: dict) -> None:
        q_id = str(data.get("id") or "")
        self._question_id = q_id
        self._options = list(data.get("options") or [])
        lines = [f"\n\n❓ **{data.get('header') or 'Ginno 提问'}**",
                 str(data.get("question") or "")]
        for i, opt in enumerate(self._options):
            lines.append(f"{i + 1}. {opt}")
        lines.append("\n（直接回复数字或文字作答；回复「跳过」跳过）")
        md = "\n".join(lines)
        self._buf.append(md)
        self._buf_len += len(md)
        await self._maybe_flush(force=True)
        # 登记待答：该会话的下一条文本消息会走 resume 而不是新 turn
        await self.adapter.register_pending_question(
            self.chat_key, self.session_id, q_id, self._options)
        timeout_s = float(self.adapter.config.get("ask_timeout_s") or 600)
        loop = asyncio.get_running_loop()
        self._ask_timer = loop.call_later(
            timeout_s, lambda: asyncio.create_task(self._ask_timeout()))

    async def _ask_timeout(self) -> None:
        from ...api.stream.resume import resume_pending_interrupt

        await self.adapter.unregister_pending_question(self.chat_key)
        resume_pending_interrupt(
            self.session_id,
            {"kind": "user_answer", "answer": "", "option_index": None,
             "skip": True},
        )

    def _cancel_ask_timer(self) -> None:
        if self._ask_timer is not None:
            self._ask_timer.cancel()
            self._ask_timer = None
        if self._question_id:
            task = asyncio.ensure_future(
                self.adapter.unregister_pending_question(self.chat_key))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # ---- 产物上行（P1）-------------------------------------------------------

    async def _send_artifact(self, path_str: str, name: str = "") -> None:
        from pathlib import Path

        from .media import upload_media

        path = Path(path_str).expanduser()
        if not path.is_file():
            return
        ext = path.suffix.lstrip(".").lower()
        if ext not in _SENDABLE:
            return
        try:
            media_type = "image" if ext in _IMAGE_EXT else "file"
            media_id = await upload_media(self.adapter.client, path,
                                          media_type=media_type)
            await self.adapter.client.respond(
                self.req_id, p.media_body(media_type, media_id))
        except Exception:  # noqa: BLE001 — 上传失败降级为路径回执
            md = f"\n📎 产物文件（企业微信上传失败，见桌面端）: `{path}`"
            self._buf.append(md)
            self._buf_len += len(md)

    # ---- 分发表 --------------------------------------------------------------

    async def on_frame(self, event: str, data: dict) -> None:
        if event == "token.delta":
            txt = str(data.get("content") or "")
            if txt:
                self._buf.append(txt)
                self._buf_len += len(txt)
                await self._maybe_flush()
        elif event == "message.end":
            await self._maybe_flush(force=True)
        elif event == "user.question":
            await self._send_question(data)
        elif event == "permission.request":
            # 高危确认企微侧自动拒绝并说明（P0 策略，同钉钉）
            from ...api.stream.resume import resume_pending_interrupt

            resume_pending_interrupt(self.session_id, {"decision": "deny"})
            tool = str(data.get("tool") or data.get("name") or "tool")
            md = f"\n\n⚠️ 高危操作 `{tool}` 在企业微信渠道默认拒绝。请到桌面端 Ginno 执行。"
            self._buf.append(md)
            self._buf_len += len(md)
            await self._maybe_flush(force=True)
        elif event == "image.emit":
            from ... import files as files_mod

            entry = files_mod.get_by_id(str(data.get("file_id") or ""))
            if entry is not None:
                await self._send_artifact(str(entry.path),
                                          str(data.get("name") or entry.name))
        elif event == "ref.emit":
            if str(data.get("kind") or "file") in ("file", "doc", "link"):
                await self._send_artifact(str(data.get("ref_id") or ""),
                                          str(data.get("name") or ""))
        elif event == "widget.emit":
            md = _widget_to_markdown(data.get("data") or {})
            if md:
                self._buf.append(f"\n\n{md}\n")
                self._buf_len += len(md) + 4
        elif event == "turn.stopped":
            await self._finalize("⏹ 已停止")
        # thinking.delta / tool.* / keepalive / artifacts.changed / …: 忽略

    async def close(self) -> None:
        """turn 结束兜底：cancel 定时器 + 未终态时补一次 finalize。"""
        self._cancel_ask_timer()
        await self._finalize()
