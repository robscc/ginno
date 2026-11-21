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


def _parked_user_question(session: dict, session_id: str) -> dict | None:
    """checkpointer 事实源:该 session 是否停在 ask_user 中断上。

    与 ws_open 的重发路径同源(get_pending_interrupt)。不用内存守卫
    (_PENDING_RESUME):守卫是进程内状态,重连/重启竞态下与 checkpointer
    脱节(2026-10-22 f5af1e9 实录:守卫为空但中断明确 parked,回答被静默
    丢弃,session 卡死)。无活任务的 parked turn 才算数——刚被回答、resume
    任务已在跑的窗口由调用方的任务检查排除。抽自 ws/steer.py(零复制:
    ws 分支与 relay_answer 工具同源调用,见 resume_question)。
    """
    try:
        graph = session.get("graph") if isinstance(session, dict) else None
        if graph is None or graph.checkpointer is None:
            return None
        pending = graph.checkpointer.get_pending_interrupt(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:
        return None
    for intr in pending or []:
        value = getattr(intr, "value", None) or intr
        if isinstance(value, dict) and value.get("kind") == "user_question":
            return value
    return None


async def resume_question(
    session_id: str,
    answer: str = "",
    option_index: int | None = None,
    skip: bool = False,
    *,
    multi: bool = False,
    answers: list[dict] | None = None,
    base_config: dict | None = None,
    arm_from_checkpoint: bool = False,
    session: dict | None = None,
    answered_by: str = "user",
    relayed_by: str | None = None,
) -> bool:
    """user_question 的统一 resume 段（relay-question 设计 ⑤，零复制抽取）。

    三路同源调用：

    - 桌面 WS ``user_answer``（ws/steer.handle_user_answer）；
    - steer 即答（ws/steer.handle_steer 的 parked-question 分支——该路
      径守卫可能丢，传 ``arm_from_checkpoint=True`` + ``session``）；
    - worker 中断透传 ``relay_answer``（worker_bus.relay）。

    成功受理（check-and-pop 通过）后统一调 ``worker_bus.relay.
    note_session_resumed`` 即销 pending_relay 登记条目、写墓碑——保证
    任何一路 resume 之后，晚到的另一路 relay 必然命中墓碑（幂等零影响）。
    """
    if arm_from_checkpoint and session_id not in _PENDING_RESUME:
        # resume_pending_interrupt 只认内存守卫;守卫丢失(重连/重启竞态)
        # 时以 checkpointer 事实武装它——steer 即答路径的存在意义。
        sess = session if session is not None else shared._SESSIONS.get(session_id)
        if sess is not None and _parked_user_question(sess, session_id) is not None:
            _PENDING_RESUME.add(session_id)
            _PENDING_KIND[session_id] = "user_question"
    value: dict = {"kind": "user_answer"}
    if multi:
        value["multi"] = True
        value["answers"] = [
            a for a in (answers or []) if isinstance(a, dict)
        ] or [{"answer": "", "option_index": None, "skip": True}]
    else:
        value.update(
            {
                "answer": str(answer or ""),
                "option_index": option_index,
                "skip": bool(skip),
            }
        )
    ok = resume_pending_interrupt(session_id, value, base_config=base_config)
    if ok:
        # 墓碑统一收口：relay 登记条目即销（无登记时零开销 dict 扫描）。
        try:
            from ...worker_bus.relay import note_session_resumed

            summary = str(answer or "")
            if multi:
                summary = "; ".join(
                    str(a.get("answer") or "") for a in value["answers"]
                )
            note_session_resumed(session_id, answered_by, summary)
        except Exception:
            _log.exception("relay_note_resumed_failed session=%s", session_id)
    return ok


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
        # 静默拒绝曾让 parked 会话的卡死无法排障(2026-10-22 f5af1e9 实录),
        # 拒绝必须留痕。id() 用于定位「同名不同对象」的身份分裂。
        _log.warning(
            "resume_rejected session=%s reason=no_pending_resume set_id=%d size=%d",
            session_id,
            id(_PENDING_RESUME),
            len(_PENDING_RESUME),
        )
        return False
    pending_kind = _PENDING_KIND.get(session_id)
    if not _payload_matches(pending_kind, resume_value):
        _log.warning(
            "resume_rejected session=%s reason=kind_mismatch pending_kind=%r value_kind=%r",
            session_id,
            pending_kind,
            (resume_value or {}).get("kind"),
        )
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
                # AgentTeam：用户代答后跑完的**真正结果**必须回主会话
                # （backlog P0-7 后半）。此前 resume 路径无任何 team 钩子，
                # 成员撞卡后代答跑出的产出永不回帖。turn_id 去重防双回帖。
                try:
                    from ...teams.lifecycle import spawn_settled as _team_settled

                    _team_settled(
                        session_id,
                        str((_cfg.get("configurable") or {}).get("turn_id") or ""),
                    )
                except Exception:
                    _log.exception("team_settle_hook_failed session=%s", session_id)

    task = asyncio.create_task(_resume_job())
    _TURN_TASKS[session_id] = task
    task.add_done_callback(
        lambda t: _TURN_TASKS.pop(session_id, None)
        if _TURN_TASKS.get(session_id) is t
        else None
    )
