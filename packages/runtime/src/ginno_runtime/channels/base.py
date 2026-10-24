"""Channel adapter layer: bridge external chat channels (DingTalk, Lark, …)
to Ginno sessions (docs/dingtalk-connector-design.md §3).

分层（与 connectors/ 的分工）：
- ``connectors/`` 负责配置、安装引导与状态上报（连接器体系契约）；
- ``channels/`` 负责协议对接与消息翻译：收渠道消息 → 路由到映射的 Ginno
  session → headless 驱动 turn（``run_channel_turn``）→ 假 WS tap
  （``ChannelTap``）把引擎帧翻译为渠道侧渲染动作。

核心代码对渠道零感知：唯一例外是 ``api/stream/resume.py`` 的公共恢复入口
（ask_user / permission 停车跨渠道复用，见设计文档 §4 F4/F5）。
"""

from __future__ import annotations

import abc
import uuid
from typing import Any

from ginno_runtime import jsonx as json

from .. import server_shared as shared

# Headless turn 协议复刻自 api/sessions.py `_run_goal_turn` 与 cli.py 的
# exec 路径；关键差异：渠道侧 **保持 interactive**（有人在渠道里应答
# ask_user），因此不调用 `_set_interactive_turn(False)`。


class ChannelTap:
    """假 WS：挂 ``server_shared._SESSION_WS[session_id]`` 收引擎 turn 帧。

    ``safe_send``（engine.py）对 socket 的全部要求是 ``send_text(str)``；
    帧格式 ``{"event": name, "turn_id"?: ..., **data}``（``_ev``）。基类解析
    帧并按 ``event`` 分发给 :meth:`on_frame`，永不抛异常（抛异常的 socket
    会被引擎剪除）。
    """

    session_id: str = ""

    async def send_text(self, payload: str) -> None:
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        ev = frame.get("event")
        if not ev:
            return
        try:
            await self.on_frame(ev, frame)
        except Exception:  # noqa: BLE001 — a bad frame must never kill the turn
            pass

    async def on_frame(self, event: str, data: dict) -> None:
        """Subclass hook: translate one engine frame into channel actions."""

    async def close(self) -> None:
        """Optional teardown when the turn/tap is detached."""


class ChannelAdapter(abc.ABC):
    """外部渠道适配器抽象（钉钉是第一个实现）。

    生命周期由对应 Connector（connectors/ 体系）驱动：``apply_config`` →
    ``start`` → （运行中 ``report_status``）→ ``stop``。
    """

    channel_name: str = "abstract"

    def __init__(self) -> None:
        self._running = False

    @abc.abstractmethod
    async def start(self, config: dict) -> None:
        """Establish the channel connection (idempotent; re-config restarts)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Drop the connection and detach every active tap."""

    def report_status(self, status: str, detail: str = "") -> None:
        """Forward a status transition to the connector registry (debounced)."""
        from ..connectors.registry import get_registry

        get_registry().report(self.channel_name, status, detail)


async def run_channel_turn(
    session: dict,
    text: str,
    tap: ChannelTap,
    *,
    attached_files: list[dict] | None = None,
    usage_source: str = "channel",
    agent_id: str | None = None,
) -> None:
    """Drive ONE headless turn on ``session`` with ``tap`` attached (F2 §7).

    Mirrors the headless protocol of ``api/sessions.py::_run_goal_turn`` /
    ``cli.py`` exec path — early-armed ``_RUNNING_TURNS``, tap attached for the
    whole turn, running-flag cleanup that KEEPS the registration when the turn
    parked at an interrupt (a pending question must stay answerable).
    """
    from ..api import stream as _stream_api  # lazy: api.stream imports sessions

    session_id = session["session_id"]
    slug = session.get("project_slug") or "default"
    agent = agent_id or session.get("agent_id")
    turn_id = uuid.uuid4().hex
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
        }
    }
    if attached_files:
        config["configurable"]["attached_files"] = attached_files
    # Early-arm: a channel `stop` during the preamble must see a live turn.
    shared._RUNNING_TURNS[session_id] = turn_id
    tap.session_id = session_id
    shared._SESSION_WS.setdefault(session_id, []).append(tap)
    try:
        await _stream_api._run_stream(None, session["graph"], config, text, session, agent)
    finally:
        # Parked-at-interrupt turns STAY registered (resume hasn't happened).
        if (
            shared._RUNNING_TURNS.get(session_id) == turn_id
            and session_id not in shared._PENDING_RESUME
        ):
            shared._RUNNING_TURNS.pop(session_id, None)
        sockets = shared._SESSION_WS.get(session_id) or []
        shared._SESSION_WS[session_id] = [s for s in sockets if s is not tap]
        await tap.close()


async def ensure_channel_session(
    session_id: str | None, *, default_agent_id: str | None = None
) -> tuple[dict | None, str | None]:
    """Return (session, session_id) — rebuild from disk if the runtime restarted.

    ``session_id=None`` creates a brand-new session via the public create API
    (``POST /api/sessions`` 的内部等价路径)。Returns (None, error) on failure.
    """
    from ..api.sessions import CreateSessionRequest, _ensure_session, create_session

    if session_id:
        s = _ensure_session(session_id)
        return (s, session_id if s else None)
    # ``workspace`` is superseded by the server (session_files_dir(slug, sid)),
    # so an empty value is fine; the create path mirrors the desktop client's.
    resp = await create_session(
        CreateSessionRequest(project_slug="default", workspace="", agent_id=default_agent_id)
    )
    if not isinstance(resp, dict) or not resp.get("session_id"):
        return None, str((resp or {}).get("error") or "create_session failed")
    sid = resp["session_id"]
    s = _ensure_session(sid)
    return (s, sid if s else None)


async def quiesce(session_id: str, slug: str, timeout_s: float = 8.0) -> dict:
    """Wait for subagents/background work to settle (cli.py 同名协议的轻封装)."""
    from ..cli import _await_quiescence

    return await _await_quiescence(session_id, slug, timeout_s)
