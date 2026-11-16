"""可选 turn 事件汇（节点侧入站进度，2026-11-17）。

背景：a2a 入站 worker 在进程内跑与本地完全相同的 turn 引擎
（``inbound_exec._run_job → stream._run_stream(ws=None)``），但节点上
``_SESSION_WS`` 里没有任何活 socket → ``safe_send`` 的所有帧被静默丢弃，
调用方只能看到「已接单 / completed」两条 notice。本模块提供**按 session
作用域**的可选 sink：注册后引擎把本来要发 socket 的同一帧交给 sink
（节点侧写进入站进度 JSONL，``kind="turn_event"``），``a2a/server`` 的
``_sse_task_stream`` tail 该文件即可把工具调用与流式文本转发给调用方。

设计约束（与下游的线上契约）：
- **默认零影响**：未注册时引擎每帧只多一次 ``dict.get``，本地桌面 turn
  行为完全不变。
- **按 session 作用域**：``dict[session_id, sink]`` 与 ``_SESSION_WS`` 同构
  （引擎既有风格；contextvar 在 spawn_bg/子任务派生下传播不可靠，弃用）。
  并发入站任务各用各的 session，互不串台；``unregister_sink`` 校验对象
  同一性，防止旧 turn 的 finally 误删新 turn 注册的 sink。
- **delta 节流**：token.delta / thinking.delta 上游已有 33ms 合帧
  （delta_coalesce.py），但写盘仍可能每 turn 上千行；这里再按
  ≥200ms 或 ≥4096 字符合并一次。**非 delta 事件先 flush 缓冲再即时写**
  （工具事件不延迟、不乱序）；``close()`` flush 残留，turn 结束不丢尾字。
  缓冲按到达顺序存片段，flush 只合并**连续同 kind** 的 delta——跨 kind
  交错（token/thinking 交替）保持原始顺序。
- **best-effort**：offer/flush/close 吞掉一切异常（照 emit_progress 风格）
  ——sink 是旁路，绝不能影响主 turn。
- ``keepalive`` 是 WS 保活帧而非 turn 事件，不落盘（每 15s 一条纯噪声）。
"""

from __future__ import annotations

import time
from typing import Callable

__all__ = [
    "TurnEventSink",
    "register_sink",
    "unregister_sink",
    "get_sink",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_CHAR_LIMIT",
]

#: delta 二次节流窗口（上游 delta_coalesce 已 33ms 合帧，这里再降一档）。
DEFAULT_INTERVAL_S = 0.2
#: 缓冲字符数阈值：长文本 burst 不等窗口，按量 flush。
DEFAULT_CHAR_LIMIT = 4096

_DELTA_EVENTS = frozenset({"token.delta", "thinking.delta"})
_SKIP_EVENTS = frozenset({"keepalive"})

# session_id → sink。与 server_shared._SESSION_WS 同款按 session 键控；
# 注册/注销由节点侧（inbound_exec._run_job）在 turn 边界 try/finally 管理。
_SINKS: dict[str, "TurnEventSink"] = {}


def register_sink(session_id: str, sink: "TurnEventSink") -> None:
    """为 session 注册事件汇（空 session_id 忽略）。"""
    if session_id:
        _SINKS[session_id] = sink


def unregister_sink(session_id: str, sink: "TurnEventSink") -> None:
    """注销事件汇。**仅当仍是同一对象时**才删——并发/续跑场景下旧 turn
    的 finally 不得误删新 turn 注册的 sink。"""
    if _SINKS.get(session_id) is sink:
        _SINKS.pop(session_id, None)


def get_sink(session_id: str) -> "TurnEventSink | None":
    """引擎 safe_send 每帧查询；未注册返回 None（默认零影响路径）。"""
    return _SINKS.get(session_id)


class TurnEventSink:
    """把 turn 事件交给 ``write(event, data)`` 回调，delta 二次节流。

    ``write`` 负责落盘形状（节点侧接 ``emit_progress("turn_event", ...)``，
    记录即 ``{"ts": float, "kind": "turn_event", "event": str, "data": dict}``）。
    本类只做节流/保序/异常隔离，不关心存储。

    用法（节点侧）::

        sink = TurnEventSink(lambda ev, data: emit_progress("turn_event", event=ev, data=data))
        register_sink(session_id, sink)
        try:
            await _run_stream(None, ...)
        finally:
            sink.close()                    # flush 残留 delta，不丢尾字
            unregister_sink(session_id, sink)
    """

    def __init__(
        self,
        write: Callable[[str, dict], None],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        char_limit: int = DEFAULT_CHAR_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._write = write
        self._interval = interval_s
        self._char_limit = char_limit
        self._clock = clock
        # 到达顺序的 (event, data) 片段队列；flush 只合并连续同 kind delta。
        self._buf: list[tuple[str, dict]] = []
        self._chars = 0
        self._last_flush = 0.0
        # 「流已开」标记：首片直发后，后续片即使缓冲已空（刚 flush 过）
        # 也必须进缓冲——否则每片都被当首片直发，节流失效。
        self._stream_open = False
        self._closed = False

    # ------------------------------------------------------------------ #
    def offer(self, event: str, data: dict) -> None:
        """接收一帧（引擎 safe_send 调用）。绝不抛。"""
        try:
            if not event or event in _SKIP_EVENTS:
                return
            if event in _DELTA_EVENTS:
                text = data.get("content") or ""
                if not text:
                    return
                if self._closed:
                    # close 后直发（照 DeltaCoalescer 语义；正常流程里
                    # close 与 unregister 紧邻，不会再有 offer）。
                    self._safe_write(event, data)
                    return
                if not self._stream_open:
                    # 首片即时（打字机首字不延迟，与上游合帧器同语义）。
                    self._stream_open = True
                    self._safe_write(event, data)
                    self._last_flush = self._clock()
                    return
                self._buf.append((event, data))
                self._chars += len(text)
                if (
                    self._chars >= self._char_limit
                    or self._clock() - self._last_flush >= self._interval
                ):
                    self.flush()
                return
            # 非 delta：先 flush 缓冲保序，再即时写（工具事件不延迟）。
            self.flush()
            self._safe_write(event, data)
        except Exception:  # noqa: BLE001 — sink 是旁路，绝不影响主 turn
            pass

    def flush(self) -> None:
        """清空缓冲：连续同 kind delta 合并成一条，其余按到达顺序逐条写。"""
        if not self._buf:
            return
        buf, self._buf = self._buf, []
        self._chars = 0
        self._last_flush = self._clock()
        i = 0
        while i < len(buf):
            event, data = buf[i]
            j = i + 1
            if event in _DELTA_EVENTS:
                parts = [data.get("content") or ""]
                while j < len(buf) and buf[j][0] == event:
                    parts.append(buf[j][1].get("content") or "")
                    j += 1
                if len(parts) > 1:
                    data = {**data, "content": "".join(parts)}
            self._safe_write(event, data)
            i = j

    def close(self) -> None:
        """turn 结束调用：flush 残留 delta（不丢尾字）。幂等，绝不抛。"""
        self._closed = True
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def _safe_write(self, event: str, data: dict) -> None:
        try:
            self._write(event, data)
        except Exception:  # noqa: BLE001 — 写失败绝不拖累 turn
            pass
