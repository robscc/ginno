"""Session-control messages: stop, subagent plan confirm/cancel,
checkpoint retry (batch 4-2 split of ``session_ws``).

Structural move only — the bodies are verbatim from the monolith, with
``continue`` turned into ``return`` now that each branch is its own
handler.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ....checkpointer import ABANDONED_TURNS
from ....goals import store as goal_store
from ....server_shared import (
    _MANAGED_TURNS,
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _RUNNING_TURNS_AT,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ....session_meta import _session_meta_patch
from ...sessions import _emit_goal_event, _first_agent_id, _start_goal_driver
from ..engine import _stop_parked_turn, _stream_graph, turn_error_fields
from .handshake import WsConn
from .invoke import reject_compacting_invoke


async def handle_plan_confirm(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    # The user confirmed (possibly edited) the /subagent 拆分 plan
    # card (P2 contract 2): spawn every subtask through the existing
    # spawn path (origin=user), then answer with ONE summary notice.
    # Off the receive loop — each spawn creates a session + graph.
    _plan_id = str(msg.get("plan_id") or "")
    _override = msg.get("subtasks")
    _override = _override if isinstance(_override, list) else None

    async def _plan_confirm_job(
        _sid=session_id, _pid=_plan_id, _ov=_override
    ) -> None:
        try:
            from ....subagent_plan import confirm_plan

            await confirm_plan(_sid, _pid, _ov)
        except Exception:
            _log.exception(
                "subagent_plan_confirm_failed session=%s plan=%s",
                _sid, _pid,
            )
            try:
                await _push_session_event(
                    _sid,
                    "notice",
                    {
                        "message": "Failed to confirm the split plan; retry or split again",
                        "i18n_key": "stream.plan_confirm_failed",
                    },
                )
            except Exception:
                pass

    spawn_bg(_plan_confirm_job())


async def handle_plan_cancel(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    # Discard the pending plan (P2 contract 2) + broadcast the
    # cancellation (P3 contract 5) so every tab's pending card
    # flips to 已取消. Idempotent end to end.
    from ....subagent_plan import cancel_plan_and_broadcast

    spawn_bg(
        cancel_plan_and_broadcast(
            session_id, str(msg.get("plan_id") or "")
        )
    )


# How long after a cooperative stop before the force-convergence watchdog
# fires. Generous on purpose: a healthy turn unwinds through its stream
# drain + heal + settle within seconds; only a turn wedged past this line
# gets cancelled. Module constant so tests can monkeypatch it down.
STOP_FORCE_DELAY_S = 60.0


def _stop_arm_force_watchdog(session: dict, session_id: str) -> None:
    """Arm the one-shot delayed convergence check for a just-stopped turn.

    Snapshot the turn's identity NOW (turn_id + task object): when the
    watchdog fires it only acts if that SAME turn is still registered — a
    turn that converged, or was replaced by a NEW invoke, is left alone
    (the successor must never be cancelled by its predecessor's stop)."""
    turn_id = _RUNNING_TURNS.get(session_id) or ""
    task = _TURN_TASKS.get(session_id)
    parked = session_id in _PENDING_RESUME
    if not turn_id and not parked and (task is None or task.done()):
        return  # idle stop: nothing to watch
    spawn_bg(_stop_force_watchdog(session, session_id, turn_id, task))


async def _stop_force_watchdog(
    session: dict, session_id: str, turn_id: str, task_at_stop: Any
) -> None:
    """Delayed backstop for a cooperative stop that went unanswered.

    Fires STOP_FORCE_DELAY_S after the user pressed ⏹. If the same turn
    (by identity) has since converged — its task finished, its
    registration popped, or a successor took over — this is a no-op. If
    it is STILL registered (wedged on a dead await, or a scheduler-managed
    subagent turn with no _TURN_TASKS entry that never observed the
    event), converge it by force:

    * ``task.cancel()`` — CancelledError is a BaseException, so the job's
      ``except Exception`` never turns it into an error card; the job's
      finally still runs its registry cleanup (identity-checked);
    * ``ABANDONED_TURNS.add(turn_id)`` BEFORE the cancel — the same
      late-write guard the stall watchdog uses, so the unwinding (or a
      gateway that finally answers an hour later) cannot persist
      checkpoints that would race a retry turn;
    * pop the registration + the stale SET stop event (a wedged turn
      never reached the finally that pops it; the next legitimate turn
      must not setdefault onto a poisoned pre-set event and insta-die);
    * broadcast the SAME terminal event the cooperative path emits
      (``turn.stopped`` {}, carried on the stopped turn_id) so every tab
      leaves the running state;
    * finalize a subagent session as stopped (``on_turn_stopped`` — no-op
      for main conversations) so its meta doesn't read running forever.
    """
    await asyncio.sleep(STOP_FORCE_DELAY_S)
    cur_task = _TURN_TASKS.get(session_id)
    task_live = (
        task_at_stop is not None
        and cur_task is task_at_stop
        and not task_at_stop.done()
    )
    reg_live = bool(turn_id) and _RUNNING_TURNS.get(session_id) == turn_id
    if not task_live and not reg_live:
        return  # converged on its own, or a new turn took over
    _log.warning(
        "turn_stop_watchdog_forced session=%s turn=%s task_live=%s "
        "(cooperative stop unanswered after %.0fs — cancelling)",
        session_id, turn_id, task_live, STOP_FORCE_DELAY_S,
    )
    if turn_id:
        # Late-write guard BEFORE the cancel: the unwinding turn (or a
        # gateway answering late) must not commit checkpoints that race
        # a retry turn (same protocol as the stall watchdog).
        ABANDONED_TURNS.add(turn_id)
    if reg_live:
        _RUNNING_TURNS.pop(session_id, None)
        _RUNNING_TURNS_AT.pop(session_id, None)
    if _MANAGED_TURNS.get(session_id) == turn_id:
        _MANAGED_TURNS.pop(session_id, None)
    # A wedged turn never reaches its finally — drop the pre-set stop
    # event here or the NEXT turn setdefaults onto it and insta-dies.
    evt = _TURN_STOP.get(session_id)
    if evt is not None and evt.is_set():
        _TURN_STOP.pop(session_id, None)
    if task_live:
        task_at_stop.cancel()
    try:
        await _push_session_event(session_id, "turn.stopped", {}, turn_id or None)
    except Exception:  # noqa: BLE001 — broadcast is best-effort
        _log.exception("stop_watchdog_event_failed session=%s", session_id)
    # Subagent (incl. scheduler-managed) terminal state: main sessions
    # no-op here; the cancelled job's own settle hook (finally) re-runs
    # the gate for every session kind.
    try:
        from ....subagent_scheduler import on_turn_stopped

        await on_turn_stopped(session_id)
    except Exception:  # noqa: BLE001
        _log.exception("stop_watchdog_subagent_hook_failed session=%s", session_id)


async def handle_stop(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    session = conn.session
    # Stop the running turn (user hit ⏹). Semantics: hard stop —
    # the current model/tool step is abandoned (ABANDONED_TURNS
    # blocks its late writes); streamed text + completed steps are
    # kept; state is healed so the next turn starts clean.
    slug = session.get("project_slug")
    # Force-convergence watchdog (2026-11-22 僵死 turn 事故): the
    # cooperative cascade below assumes the turn eventually observes
    # the stop event and finalizes itself — a turn wedged on a dead
    # await (hung gateway, stuck lock, subagent scheduler turn with
    # no _TURN_TASKS entry) never does, and the registration then
    # reads "running" forever (badge, busy gates, quit check). Arm a
    # ONE-SHOT delayed check while the stop is being served: if the
    # SAME turn (identity: turn_id + task object snapshot) is still
    # registered when it fires, converge it by force.
    _stop_arm_force_watchdog(session, session_id)
    # An active goal would auto-continue ~3s after the stopped
    # turn unwinds — pause it first so "stop" means stop. Do NOT
    # cancel the driver task: it awaits the goal turn inline, and
    # task.cancel() would inject CancelledError into the running
    # turn (the swallow-prone cancellation this design avoids).
    try:
        goal = goal_store.get_goal(slug, session_id)
        if goal and goal.get("status") == goal_store.STATUS_ACTIVE:
            paused = goal_store.update_status(
                slug,
                session_id,
                goal_store.STATUS_PAUSED,
                expected_goal_id=goal.get("goal_id"),
            )
            if paused:
                await _emit_goal_event(slug, session_id, paused)
    except Exception:
        _log.exception("stop_goal_pause_failed session=%s", session_id)
    # Subagent stop cascade (subagent-design.md §5.6 配套规则): every
    # running descendant of a stopped running/waiting subagent stops
    # with it — cooperative events only; each turn's own machinery
    # finalizes its status. Also covers the WAITING subagent whose
    # own turn is not live: it is marked stopped here. Scoped to
    # subagent sessions: stopping a MAIN conversation's turn must
    # not kill its independently running subagents.
    if session.get("type") == "subagent":
        try:
            from ....subagent_scheduler import (
                on_stop_if_waiting,
                stop_descendants,
            )

            async def _stop_cascade(_sid=session_id) -> None:
                # Off the receive loop: the cascade may heal a
                # parked descendant's checkpoint (file IO) and
                # finalize waiting ones.
                try:
                    await stop_descendants(_sid)
                except Exception:
                    _log.exception(
                        "subagent_stop_cascade_failed session=%s", _sid
                    )

            spawn_bg(_stop_cascade())
            spawn_bg(on_stop_if_waiting(session_id))
        except Exception:
            _log.exception(
                "subagent_stop_cascade_failed session=%s", session_id
            )
    if session_id in _PENDING_RESUME:
        # Parked at a permission/version/ask_user interrupt: no
        # live task — heal + broadcast directly. Check-and-discard
        # (sync, so exactly one of two tabs wins; the loser no-ops).
        _PENDING_RESUME.discard(session_id)
        _PENDING_KIND.pop(session_id, None)
        tid = _RUNNING_TURNS.pop(session_id, "")
        spawn_bg(_stop_parked_turn(session, session_id, tid))
        return
    _t = _TURN_TASKS.get(session_id)
    _live = (_t is not None and not _t.done()) or session_id in _RUNNING_TURNS
    if _live:
        evt = _TURN_STOP.get(session_id)
        if evt is None:
            evt = _TURN_STOP[session_id] = asyncio.Event()
        evt.set()
    # idle → no-op (never leave a set event for the next turn)


async def handle_retry_from_checkpoint(conn: WsConn, msg: dict) -> None:
    ws = conn.ws
    session_id = conn.session_id
    session = conn.session
    config = conn.config
    # Retry the failed turn from its latest checkpoint (P2):
    # instead of re-executing from the start, resume from the last
    # successful state. This preserves tool calls and intermediate
    # results, avoiding redundant work on long-running turns.
    turn_id = msg.get("turn_id") or str(uuid.uuid4())
    if await reject_compacting_invoke(conn, turn_id):
        return
    # Client-facing id (the error card's retry round-trips it). If
    # the failed attempt was ABANDONED (stall watchdog / user stop)
    # its id is poisoned for checkpoint writes — FileCheckpointer
    # would silently refuse ALL state from this retry, so the retry
    # would run yet persist nothing. Execute under a derived id and
    # carry _ui_turn_id so every client-facing surface (events,
    # last_error) stays on the original.
    ui_turn_id_r = turn_id
    if turn_id in ABANDONED_TURNS:
        turn_id = f"{ui_turn_id_r}--r{uuid.uuid4().hex[:6]}"
    _busy = _TURN_TASKS.get(session_id)
    if _busy is not None and not _busy.done():
        await _push_session_event(
            session_id,
            "notice",
            {
                "message": "A turn is already running; please wait for it to finish",
                "i18n_key": "stream.busy_wait",
            },
            turn_id,
        )
        return
    slug = session.get("project_slug")
    # Clear last_error and announce the retry
    _session_meta_patch(slug, session_id, {"last_error": {}})
    # A FAILED subagent was already finalized terminal ("failed");
    # revive it to running so the retry's settle gate re-runs and a
    # successful retry actually injects its result (minor-6).
    try:
        from ....subagent_scheduler import revive_failed_for_retry

        await revive_failed_for_retry(session_id)
    except Exception:
        _log.exception(
            "subagent_retry_revive_failed session=%s", session_id
        )
    _TURN_STOP.setdefault(session_id, asyncio.Event())
    retry_agent = session.get("agent_id") or _first_agent_id()
    retry_config = {
        **config,
        "configurable": {
            **config["configurable"],
            "agent_id": retry_agent,
            "turn_id": turn_id,
            "_ui_turn_id": ui_turn_id_r,
        },
    }
    _log.info(
        "retry_from_checkpoint session=%s turn=%s ui_turn=%s agent=%s",
        session_id, turn_id, ui_turn_id_r, retry_agent,
    )

    async def _checkpoint_retry_job(
        _cfg=retry_config, _tid=turn_id, _ui_tid=ui_turn_id_r
    ) -> None:
        # Same capture-before-cleanup protocol as _turn_job: the
        # settle gate needs the object to read is_set() after the
        # finally pops the registry entry.
        _job_stop_evt = _TURN_STOP.get(session_id)
        try:
            # Broadcast like every other turn frame (the stream
            # below already does): the socket that asked for the
            # retry may have reconnected while the job waited on
            # the turn lock.
            await _push_session_event(
                session_id,
                "turn.start",
                {"turn_id": _ui_tid, "agent_id": retry_agent or "", "name": retry_agent or "Agent", "from_checkpoint": True},
            )
            async with _turn_lock(session_id):
                # 审计 B：从 checkpoint 重跑也是真正发起 turn——归零
                # mid-turn 压缩节流（模块级 dict 状态，防跨 turn 泄漏）。
                try:
                    from ....graph import reset_mid_compact_state

                    reset_mid_compact_state(session_id)
                except Exception:  # noqa: BLE001 — best-effort
                    pass
                # Pass input_state=None to resume from latest checkpoint
                await _stream_graph(
                    session["graph"],
                    _cfg,
                    input_state=None,  # Resume from checkpoint
                    command=None,
                )
            _start_goal_driver(session_id)
        except Exception as e:
            _log.exception("checkpoint_retry_error session=%s turn=%s", session_id, _tid)
            await _push_session_event(
                session_id,
                "error",
                {
                    **turn_error_fields(e),
                },
            )
        finally:
            if _TURN_TASKS.get(session_id) is asyncio.current_task():
                _TURN_TASKS.pop(session_id, None)
            _TURN_STOP.pop(session_id, None)
            # Subagent completion gate for the retried segment — a
            # retry settling is a turn settling like any other; the
            # failed attempt's "failed" meta was revived to running
            # before the job spawned, so a successful retry here
            # re-runs the gate and injects its result (minor-6).
            # The gate keys _turn_last_error on the CLIENT-facing
            # id (_stream_graph persists last_error under
            # ``_ui_turn_id``) — passing the derived exec ``_tid``
            # would miss the record whenever the original turn was
            # abandoned (its id is the ``--r`` derivation base), and
            # a permanently failed retry would settle "done" with a
            # phantom summary injected into the parent.
            if session_id not in _PENDING_RESUME:
                try:
                    from ....subagent_scheduler import on_turn_settled

                    spawn_bg(
                        on_turn_settled(session_id, _job_stop_evt, _ui_tid)
                    )
                except Exception:
                    _log.exception(
                        "subagent_settle_hook_failed session=%s",
                        session_id,
                    )

    task = asyncio.create_task(_checkpoint_retry_job())
    _TURN_TASKS[session_id] = task
    task.add_done_callback(
        lambda t: _TURN_TASKS.pop(session_id, None)
        if _TURN_TASKS.get(session_id) is t
        else None
    )
