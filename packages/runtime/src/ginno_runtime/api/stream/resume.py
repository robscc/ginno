"""Public resume entry for parked interrupt turns.

提炼自 ``api/stream/ws.py`` 的 ``_spawn_resume`` 闭包与 ``user_answer`` /
``permission_response`` 分支（dingtalk-connector-design.md §4 F4）：桌面 WebSocket
与外部渠道（钉钉 ChannelAdapter 等）共用同一套停车/恢复协议。

协议要点（与原 ws.py 行为逐字段等价）：
- 停车寄存器在 ``server_shared``：``_PENDING_RESUME``（set）+ ``_PENDING_KIND``（dict）；
- 受理前 **peek 不 pop**：payload 与停车种类不匹配时拒绝，保持停车可答；
- resume 是独立 task：序列化在 ``_turn_lock(session_id)`` 后面（暂停 turn 的
  stream drain 未落 checkpoint 前不得恢复，否则重跑被中断的工具）；
- ``base_config``：桌面 WS 路径传入其捕获的 turn config（行为不变）；外部渠道
  传 ``None``，由本模块按 session 重建（resume 本质是新 turn 段，turn_id 重新生成）。
"""

from __future__ import annotations

import asyncio
import uuid

from ... import server_shared as shared
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)

# Interrupt kinds whose resume payload is routed through this module.
_QUESTION_KINDS = {"user_question": "user_answer"}
_DECISION_KINDS = ("permission_request", "version_propose")


def _payload_matches(pending_kind: str | None, resume_value: dict) -> bool:
    """peek-only validation: never pop on a mismatch (stale msg stays answerable)."""
    if pending_kind == "user_question":
        return resume_value.get("kind") == "user_answer"
    if pending_kind in _DECISION_KINDS:
        return "decision" in resume_value
    return False


def _rebuild_config(session: dict) -> dict:
    """External-channel resume has no captured turn config; rebuild one.

    Mirrors the headless protocol in api/sessions.py ``_run_goal_turn`` /
    cli.py (thread_id + project_slug + agent_id + fresh turn_id).
    """
    from ..sessions import _first_agent_id

    return {
        "configurable": {
            "thread_id": session["session_id"],
            "project_slug": session.get("project_slug") or "default",
            "agent_id": session.get("agent_id") or _first_agent_id(),
            "turn_id": uuid.uuid4().hex,
            "usage_source": "channel_resume",
        }
    }


def resume_pending_interrupt(
    session_id: str, resume_value: dict, *, base_config: dict | None = None
) -> bool:
    """Resume a parked interrupt turn from ANY channel. Returns True if accepted.

    Callers:
    - desktop WS receive loop (``api/stream/ws.py``) passes its captured config;
    - external channel adapters (``ginno_runtime.channels``) pass ``None``.
    """
    if session_id not in _PENDING_RESUME:
        return False
    pending_kind = _PENDING_KIND.get(session_id)
    if not _payload_matches(pending_kind, resume_value):
        return False
    # Accept: clear the park BEFORE spawning so a second answer is rejected.
    _PENDING_RESUME.discard(session_id)
    _PENDING_KIND.pop(session_id, None)
    _spawn_resume_task(session_id, resume_value, base_config)
    return True


def _spawn_resume_task(session_id: str, resume_value: dict, base_config: dict | None) -> None:
    """Spawn the resume as its own task — logic lifted verbatim from ws.py's
    ``_spawn_resume`` closure (lock serialization, stop-event capture, goal
    driver restart, subagent settle hook)."""
    from ..sessions import _first_agent_id, _start_goal_driver
    from .turn import _run_resume

    session = shared._SESSIONS.get(session_id)
    if session is None:
        _log.error("resume_no_session session=%s", session_id)
        return
    config = base_config if base_config is not None else _rebuild_config(session)
    resume_agent = session.get("agent_id") or _first_agent_id()
    resume_config = {
        **config,
        "configurable": {**config["configurable"], "agent_id": resume_agent},
    }
    _TURN_STOP.setdefault(session_id, asyncio.Event())
    _resume_stop_evt = _TURN_STOP.get(session_id)

    async def _resume_job(_cfg=resume_config, _val=resume_value) -> None:
        try:
            # Serialize behind _turn_lock: the paused turn's task may still be
            # draining its stream generator; resuming before the suspension
            # checkpoint lands would re-run the interrupted tool from scratch.
            async with _turn_lock(session_id):
                await _run_resume(None, session["graph"], _cfg, _val)
            _start_goal_driver(session_id)  # success path only
        except Exception as e:
            _log.exception("resume_error session=%s", session_id)
            from .engine import turn_error_fields

            await _push_session_event(session_id, "error", turn_error_fields(e))
        finally:
            if _TURN_TASKS.get(session_id) is asyncio.current_task():
                _TURN_TASKS.pop(session_id, None)
            _TURN_STOP.pop(session_id, None)
            # Subagent completion gate for the resumed segment.
            if session_id not in _PENDING_RESUME:
                try:
                    from ...subagent_scheduler import on_turn_settled

                    spawn_bg(
                        on_turn_settled(
                            session_id,
                            _resume_stop_evt,
                            str((_cfg.get("configurable") or {}).get("turn_id") or ""),
                        )
                    )
                except Exception:
                    _log.exception("subagent_settle_hook_failed session=%s", session_id)

    task = asyncio.create_task(_resume_job())
    _TURN_TASKS[session_id] = task
    task.add_done_callback(
        lambda t: _TURN_TASKS.pop(session_id, None)
        if _TURN_TASKS.get(session_id) is t
        else None
    )
