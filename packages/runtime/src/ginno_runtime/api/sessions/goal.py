"""sessions goal 域：goal 端点（GET/PUT/DELETE）+ continuation driver。

driver 语义见 goal-design.md §4.3.3：每会话一条 asyncio 任务，turn 收尾
后 headless 注入续跑消息；用户 turn 永远优先（turn lock + idle 等待），
permission 打断停续，agent 切换自动暂停，无 turn 数上限（上下文交给既有
auto-compaction）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

from ...goals import events as goal_events
from ...goals import store as goal_store
from ...goals import templates as goal_templates
from ...lang import t
from ...server_shared import (
    _GOAL_DRIVERS,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _log,
    _push_session_event,
    _turn_lock,
)
from ...session_meta import _find_meta
from ...tools.ask_tools import reset_interactive as _reset_interactive_turn
from ...tools.ask_tools import set_interactive as _set_interactive_turn
from ._base import _ensure_session, _first_agent_id, router


# ---- Goal continuation driver (goal-design.md §4.3.3) ---------------------
# One asyncio task per session with an active goal. After every turn ends and
# the session goes idle, it injects a continuation message and starts the next
# turn HEADLESSLY (no client socket required — the user may have closed the
# window). Guards: user turns always win (turn lock + idle waits), pending
# permission interrupts stall continuation, a goal does not follow an agent
# switch (auto-pause). There is deliberately NO turn-count cap: context size
# is managed by the existing auto-compaction (E3).

GOAL_GRACE_S = 3.0  # pause between turns so the user can interject


async def _emit_goal_event(
    slug: str, session_id: str, goal: dict | None, turn_id: str | None = None
) -> None:
    if goal is None:
        await _push_session_event(session_id, "goal.cleared", {})
    else:
        await _push_session_event(session_id, "goal.updated", {"goal": goal}, turn_id)


async def _archive_complete_goal(slug: str, session_id: str) -> None:
    """Lifecycle finish (2026-11-16): move a COMPLETE goal to history + clear
    the chip. Idempotent (archive_goal no-ops once the record is gone).

    Call sites are ordered so the FINAL turn's accounting is never lost:
    the driver archives AFTER account_turn (goal-turn path); the turn_done
    hook archives only when no driver owns the session (user-turn path).
    """
    try:
        archived = goal_store.archive_goal(slug, session_id)
        if archived:
            await _push_session_event(session_id, "goal.archived", {"goal": archived})
    except Exception:  # noqa: BLE001 — 归档绝不带崩 driver/turn
        _log.exception("goal_archive_failed session=%s", session_id)


def _stop_goal_driver(session_id: str) -> None:
    task = _GOAL_DRIVERS.pop(session_id, None)
    if task and not task.done():
        task.cancel()


def _start_goal_driver(session_id: str) -> None:
    """Ensure a driver loop runs for the session when its goal is active."""
    try:
        s = _SESSIONS.get(session_id)
        if not s:
            return
        goal = goal_store.get_goal(s["project_slug"], session_id)
        if not goal or goal.get("status") != goal_store.STATUS_ACTIVE:
            _stop_goal_driver(session_id)
            return
        task = _GOAL_DRIVERS.get(session_id)
        if task and not task.done():
            return
        _GOAL_DRIVERS[session_id] = asyncio.get_running_loop().create_task(
            _goal_driver_loop(session_id)
        )
    except RuntimeError:
        # called from a sync context without a running loop (shouldn't happen
        # in the sidecar, but never let goal bookkeeping break the caller)
        pass


def _goal_listener(slug: str, session_id: str, goal: dict | None) -> None:
    """Sync bridge from goal tools: broadcast the change + reconcile driver."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_emit_goal_event(slug, session_id, goal))
    if goal and goal.get("status") == goal_store.STATUS_ACTIVE:
        _start_goal_driver(session_id)
    else:
        _stop_goal_driver(session_id)


goal_events.register_goal_listener(_goal_listener)


_USAGE_LIMITED_MARKERS = (
    "rate limit", "rate_limit", "ratelimit", "429", "quota",
    "insufficient_quota", "usage limit", "usage_limited", "overloaded",
    "billing", "credit",
)


def _goal_error_status(err_msg: str) -> str:
    """Map a failed continuation turn's error to the goal stop-status.

    Provider rate-limit / quota / billing failures are external capacity
    problems the agent cannot work around → ``usage_limited``; anything else
    (model/tool crash) → ``blocked``. Prevents the driver from error-looping.
    """
    low = (err_msg or "").lower()
    if any(m in low for m in _USAGE_LIMITED_MARKERS):
        return goal_store.STATUS_USAGE_LIMITED
    return goal_store.STATUS_BLOCKED


def _turn_last_error(session_id: str, turn_id: str) -> str | None:
    """The error message if ``turn_id`` ended in a persisted failure, else None."""
    found = _find_meta(session_id)
    if not found:
        return None
    meta, _ = found
    err = meta.get("last_error") or None
    if isinstance(err, dict) and err.get("turn_id") == turn_id:
        return str(err.get("message") or "")
    return None


async def _run_goal_turn(session: dict, goal: dict, turn_id: str) -> None:
    """Run ONE headless continuation turn for the session's goal."""
    # Lazy import: api.stream imports this module (_ensure_session et al.), so
    # a top-level import here would be a cycle. Resolved at call time, when
    # both modules are fully initialized.
    from .. import stream as _stream_api

    session_id = session["session_id"]
    slug = session["project_slug"]
    agent_id = goal.get("agent_id") or session.get("agent_id") or _first_agent_id()
    text = goal_templates.render_continuation(goal)
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            # Usage telemetry tags continuation turns as source="goal"
            # (usage-stats-design.md §3.6) so they are distinguishable from
            # user-driven chat in the usage log.
            "usage_source": "goal",
        }
    }
    _log.info(
        "goal_continuation session=%s turn=%s goal_turn=%d",
        session_id,
        turn_id,
        int(goal.get("turns_used", 0)) + 1,
    )
    # Early-arm the running flag: _stream_graph only registers the turn once
    # streaming starts, but _run_stream's preamble (compaction etc.) can take
    # seconds — a user `stop` arriving in that window must see a live turn.
    # Goal turns don't pass the WS loop, so this flag is the stop handler's
    # only liveness signal for them.
    _RUNNING_TURNS[session_id] = turn_id
    # Nobody is watching a continuation turn. ask_user would park it until the
    # stop signal with no one to answer, so the tool is told it is headless and
    # refuses instead — the model decides and states its assumption.
    ask_tok = _set_interactive_turn(False)
    try:
        await _stream_api._run_stream(None, session["graph"], config, text, session, agent_id)
    finally:
        _reset_interactive_turn(ask_tok)
        # Normal/error paths already pop inside _stream_graph; a turn parked
        # at an interrupt must STAY registered (resume hasn't happened yet).
        if (
            _RUNNING_TURNS.get(session_id) == turn_id
            and session_id not in _PENDING_RESUME
        ):
            _RUNNING_TURNS.pop(session_id, None)


def _goal_interrupted(session_id: str) -> bool:
    """True while continuation must not start: a user turn runs, a permission
    interrupt is pending, or the session vanished."""
    return (
        session_id in _RUNNING_TURNS
        or session_id in _PENDING_RESUME
        or session_id not in _SESSIONS
    )


async def _goal_driver_loop(session_id: str) -> None:
    slug: str | None = None
    try:
        while True:
            session = _SESSIONS.get(session_id) or _ensure_session(session_id)
            if not session:
                return
            # 远程会话二次守卫（2026-11-18 远程安全网 C1-P0）：API 层已拒绝
            # 设置 goal，这里兜住一切绕过路径（goal 在挂 peer 前已设置、
            # ws turn 收口的 _start_goal_driver 重挂、store 文件被外部写入）。
            # remote_peer 会话的 turn 在对端执行——complete 信号落对端 store，
            # 本地 driver 只会无限派活且无预算熔断，直接退出。
            if str(session.get("remote_peer") or "").strip():
                _log.warning("goal_driver_remote_blocked session=%s", session_id)
                return
            slug = session["project_slug"]
            goal = goal_store.get_goal(slug, session_id)
            if not goal or goal.get("status") != goal_store.STATUS_ACTIVE:
                # A user turn may have marked the goal complete while this
                # driver was waiting — archive it on the way out.
                if goal and goal.get("status") == goal_store.STATUS_COMPLETE:
                    await _archive_complete_goal(slug, session_id)
                return
            # Goal does not follow an agent switch (review decision 5): the
            # driver auto-pauses instead of continuing under the new agent.
            if (
                goal.get("agent_id")
                and session.get("agent_id")
                and session["agent_id"] != goal["agent_id"]
            ):
                paused = goal_store.update_status(
                    slug, session_id, goal_store.STATUS_PAUSED,
                    expected_goal_id=goal["goal_id"],
                )
                if paused:
                    await _emit_goal_event(slug, session_id, paused)
                return
            goal_id = goal["goal_id"]

            # Wait until the session is idle (user turn / permission interrupt
            # in flight). Re-check the goal every lap: a pause/clear/replace
            # while waiting must stop this loop.
            while _goal_interrupted(session_id):
                await asyncio.sleep(0.4)
                cur = goal_store.get_goal(slug, session_id)
                if (
                    not cur
                    or cur.get("goal_id") != goal_id
                    or cur.get("status") != goal_store.STATUS_ACTIVE
                ):
                    if cur and cur.get("goal_id") == goal_id and cur.get("status") == goal_store.STATUS_COMPLETE:
                        await _archive_complete_goal(slug, session_id)
                    return

            # Grace period — the user can interject; the invoke path takes the
            # turn lock immediately, and the re-checks below notice the turn.
            waited = 0.0
            step = 0.5
            while waited < GOAL_GRACE_S:
                await asyncio.sleep(step)
                waited += step
                cur = goal_store.get_goal(slug, session_id)
                if (
                    not cur
                    or cur.get("goal_id") != goal_id
                    or cur.get("status") != goal_store.STATUS_ACTIVE
                ):
                    if cur and cur.get("goal_id") == goal_id and cur.get("status") == goal_store.STATUS_COMPLETE:
                        await _archive_complete_goal(slug, session_id)
                    return
                if _goal_interrupted(session_id):
                    break  # back to the idle-wait loop above

            started = time.time()
            turn_id = str(uuid.uuid4())
            async with _turn_lock(session_id):
                # Final guards under the lock — a user turn may have raced in.
                if _goal_interrupted(session_id):
                    continue
                cur = goal_store.get_goal(slug, session_id)
                if (
                    not cur
                    or cur.get("goal_id") != goal_id
                    or cur.get("status") != goal_store.STATUS_ACTIVE
                ):
                    return
                session = _SESSIONS.get(session_id)
                if not session:
                    return
                await _run_goal_turn(session, cur, turn_id)

            # A failed continuation turn must STOP the loop (no error-looping):
            # map the persisted failure to usage_limited / blocked (P2-11).
            err_msg = _turn_last_error(session_id, turn_id)
            if err_msg is not None:
                stopped = goal_store.update_status(
                    slug, session_id, _goal_error_status(err_msg),
                    expected_goal_id=goal_id,
                )
                if stopped:
                    await _emit_goal_event(slug, session_id, stopped)
                return

            # Light accounting (design §4.3.4) + usage-visible event.
            accounted = goal_store.account_turn(
                slug, session_id, time.time() - started, expected_goal_id=goal_id
            )
            if accounted:
                await _emit_goal_event(slug, session_id, accounted)
            # Lifecycle finish: archive AFTER the final accounting so the
            # terminating turn's usage lands in the history record (design
            # §4.3.4 counts the turn that marks complete).
            cur = goal_store.get_goal(slug, session_id)
            if cur and cur.get("status") == goal_store.STATUS_COMPLETE:
                await _archive_complete_goal(slug, session_id)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("goal_driver_error session=%s", session_id)
        # A crashing driver must not leave the goal silently "running": mark
        # it blocked so the user sees state instead of an invisible loop.
        if slug:
            try:
                stopped = goal_store.update_status(slug, session_id, goal_store.STATUS_BLOCKED)
                if stopped:
                    await _emit_goal_event(slug, session_id, stopped)
            except Exception:
                pass
    finally:
        if _GOAL_DRIVERS.get(session_id) is asyncio.current_task():
            _GOAL_DRIVERS.pop(session_id, None)


# ---- session goal (goal-design.md §4.4) -----------------------------------


def _goal_slug(session_id: str) -> str | None:
    """Resolve the project slug for a session without building its graph."""
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None


def _goal_session_remote(session_id: str) -> bool:
    """会话是否挂 remote_peer（goal fail-closed 判定，2026-11-18 远程安全网）。

    内存注册表优先；未加载的会话回落磁盘 meta——PUT goal 常发生在会话
    尚未建图时（侧栏直接对新会话设目标）。任何一侧带 remote_peer 即真。
    """
    s = _SESSIONS.get(session_id)
    if s is not None:
        return bool(str(s.get("remote_peer") or "").strip())
    found = _find_meta(session_id)
    return bool(str(((found[0] if found else None) or {}).get("remote_peer") or "").strip())


def _goal_agent(session_id: str, slug: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s.get("agent_id")
    meta, _ = _find_meta(session_id) or ({}, slug)
    return (meta or {}).get("agent_id")


@router.get("/api/sessions/{session_id}/goal")
async def get_session_goal(session_id: str) -> dict:
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    return {"ok": True, "goal": goal_store.get_goal(slug, session_id)}


@router.put("/api/sessions/{session_id}/goal")
async def set_session_goal(session_id: str, req: dict) -> dict:
    """Create / replace the objective and/or change status.

    body: {objective?, status?, confirm?}
    * objective + existing UNFINISHED goal + no confirm → 409 needs_confirm.
    * status accepts only user actions: "active" (resume) | "paused".
    * remote_peer 会话 → 一律拒绝（2026-11-18 远程安全网 C1-P0）：goal 的
      执行与 complete 信号都在对端节点，本地 driver 只会无限向远端派活
      且永无预算熔断。
    """
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    if _goal_session_remote(session_id):
        # API 层 fail-closed（driver 入口另有二次守卫）：语义对齐
        # needs_confirm 的 409 形态，走 ok=False 信封 + remote 标记，前端
        # GoalChip 按既有 error 分支展示双语原因。
        return {
            "ok": False,
            "remote": True,
            "error": t(
                "Remote sessions don't support goals: the objective is owned by "
                "the peer node. Use a local session for goal-driven work.",
                "远程会话暂不支持 goal：目标由对端节点管理。目标驱动的工作请使用本地会话。",
            ),
        }
    objective = req.get("objective")
    status = req.get("status")
    confirm = bool(req.get("confirm"))
    existing = goal_store.get_goal(slug, session_id)

    if objective is not None:
        try:
            if goal_store.is_open(existing) and not confirm:
                return {
                    "ok": False,
                    "needs_confirm": True,
                    "goal": existing,
                    "error": "session has an unfinished goal",
                }
            goal = goal_store.replace_goal(
                slug, session_id, objective, agent_id=_goal_agent(session_id, slug)
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        await _emit_goal_event(slug, session_id, goal)
        _start_goal_driver(session_id)
        # Running-turn awareness (2026-11-16): a goal set mid-turn is invisible
        # to the running model — goal continuation only fires when the session
        # is idle, so users perceived "set a goal while running, nothing
        # happened". Steer the new objective in so the current turn knows about
        # it at its next tool boundary (the driver still owns continuation).
        if session_id in _RUNNING_TURNS:
            from ... import server_shared

            _obj = goal["objective"]
            _obj_short = _obj if len(_obj) <= 500 else _obj[:500] + "…"
            server_shared.steer_enqueue(
                session_id,
                {
                    "steer_id": f"goal-set-{goal['goal_id']}",
                    "text": (
                        f"[System] The user just set a session goal: {_obj_short}\n"
                        "Finish your current work first; once this turn settles, "
                        "the goal driver will pursue the goal automatically.\n"
                        f"【系统】用户刚设置了会话目标：{_obj_short}\n"
                        "先完成当前工作；本回合结束后 goal driver 会自动续跑该目标。"
                    ),
                },
            )
        return {"ok": True, "goal": goal}

    if status is not None:
        if status not in (goal_store.STATUS_ACTIVE, goal_store.STATUS_PAUSED):
            return {
                "ok": False,
                "error": "status must be 'active' or 'paused' (complete/blocked are model-set)",
            }
        if not existing:
            return {"ok": False, "error": "no goal set"}
        if status == goal_store.STATUS_ACTIVE and existing["status"] == goal_store.STATUS_COMPLETE:
            return {"ok": False, "error": "completed goal cannot be resumed; set a new objective"}
        goal = goal_store.update_status(
            slug, session_id, status, expected_goal_id=existing["goal_id"]
        )
        if not goal:
            return {"ok": False, "error": "goal changed concurrently; refresh"}
        await _emit_goal_event(slug, session_id, goal)
        if status == goal_store.STATUS_ACTIVE:
            _start_goal_driver(session_id)
        else:
            _stop_goal_driver(session_id)
        return {"ok": True, "goal": goal}

    return {"ok": False, "error": "nothing to do: pass objective and/or status"}


@router.delete("/api/sessions/{session_id}/goal")
async def clear_session_goal(session_id: str) -> dict:
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    cleared = goal_store.clear_goal(slug, session_id)
    if cleared:
        _stop_goal_driver(session_id)
        await _emit_goal_event(slug, session_id, None)
    return {"ok": True, "cleared": cleared}
