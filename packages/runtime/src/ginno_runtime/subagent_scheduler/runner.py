"""Turn runner + result injection (§5.5): the scheduler-managed headless turn
(standalone and under-lock variants), the merged idle-parent injection queue
with its coalescing flush, and the wake turn that opens on an idle parent."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

_NS = sys.modules[__package__]  # package namespace: all former module globals


# --------------------------------------------------------------------------- #
# turn runner (background; pattern: _run_goal_turn / _WF_RUN_TASKS)
# --------------------------------------------------------------------------- #

async def _run_managed_turn(
    session_id: str,
    text: str,
    extra_kwargs: dict | None,
    stop_evt: asyncio.Event | None = None,
) -> None:
    """Run ONE headless turn for a scheduler-managed session (child brief, or a
    result-injection wake into an idle parent). The caller must NOT hold the
    turn lock. Afterwards the completion gate runs if the session is a subagent."""
    from ..api.sessions import _ensure_session, _first_agent_id  # lazy: cycle
    from ..api.stream import _run_stream  # lazy: cycle

    # A managed turn must never run inside the spawning turn's LLM run context
    # (2026-10-01 串线：父 turn 的 messages 流会把子代理的 token 当自己的推出去).
    # spawn_bg already clears it; this covers direct callers too.
    _NS.server_shared.clear_inherited_llm_context()

    session = _NS._SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        _NS._log.error("subagent_turn_missing_session session=%s", session_id)
        _NS._TURN_STOP.pop(session_id, None)
        return
    if stop_evt is None:
        stop_evt = _NS._TURN_STOP.setdefault(session_id, _NS.asyncio.Event())
    if stop_evt.is_set():
        # Killed in the spawn window: never run, settle as stopped.
        _NS._TURN_STOP.pop(session_id, None)
        await _NS.on_turn_stopped(session_id)
        return
    turn_id = str(_NS.uuid.uuid4())
    agent_id = session.get("agent_id") or _first_agent_id()
    slug = session["project_slug"]
    # Distinguishable from user chat in the usage log.
    _found = _NS._find_meta(session_id)
    usage_source = "subagent" if _NS._is_subagent(_found[0] if _found else session) else "chat"
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
            # 本地 WS 回显（engine turn.start 带 text）：开着会话页时注入
            # 信封（worker assign / 子代理回传）以输入气泡+折卡实时可见，
            # 不再等刷新后 history 补上（2026-10-30 e7336e02 观感问题）。
            "echo_user": True,
        }
    }
    _NS._RUNNING_TURNS[session_id] = turn_id
    # Mark the turn as scheduler-managed for its whole streaming lifetime: the
    # invoke busy check in ws.py can't see it via _TURN_TASKS (it never spawns
    # through a socket) and must not queue a user message on the turn lock for
    # the rest of this (possibly minutes-long) turn. Popped unconditionally in
    # the finally — a parked-at-interrupt turn is resumed by the WS path, and a
    # free-text invoke aimed at a parked child is lock-queued like today.
    _NS._MANAGED_TURNS[session_id] = turn_id
    try:
        async with _turn_lock_of(session_id):
            await _run_stream(
                None, session["graph"], config, text, session, agent_id,
                user_extra_kwargs=extra_kwargs,
            )
    finally:
        if _NS._MANAGED_TURNS.get(session_id) == turn_id:
            _NS._MANAGED_TURNS.pop(session_id, None)
        if _NS._RUNNING_TURNS.get(session_id) == turn_id and session_id not in _NS._PENDING_RESUME:
            _NS._RUNNING_TURNS.pop(session_id, None)
        _NS._TURN_STOP.pop(session_id, None)
        # A turn parked at a permission/ask_user interrupt settles later —
        # via the resume path or the parked-stop path (stream.py hooks).
        if session_id not in _NS._PENDING_RESUME:
            try:
                await _NS.on_turn_settled(session_id, stop_evt, turn_id)
            except Exception:
                _NS._log.exception("subagent_settle_failed session=%s", session_id)


def _turn_lock_of(session_id: str):
    from ..server_shared import _turn_lock

    return _turn_lock(session_id)


async def _run_managed_turn_locked(
    session_id: str, text: str, extra_kwargs: dict | None, stop_evt: asyncio.Event
) -> None:
    """_run_managed_turn for a caller already holding the turn lock."""
    from ..api.sessions import _ensure_session, _first_agent_id  # lazy: cycle
    from ..api.stream import _run_stream

    _NS.server_shared.clear_inherited_llm_context()

    session = _NS._SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        _NS._TURN_STOP.pop(session_id, None)
        return
    turn_id = str(_NS.uuid.uuid4())
    agent_id = session.get("agent_id") or _first_agent_id()
    # The wake channel also serves MAIN conversations (an idle parent waking to
    # absorb a result): only a subagent session's usage is tagged "subagent"
    # (same rule as _run_managed_turn).
    _found = _NS._find_meta(session_id)
    usage_source = "subagent" if _NS._is_subagent(_found[0] if _found else session) else "chat"
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": session["project_slug"],
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
            # 同 _run_managed_turn：注入信封随 turn.start 回显给本地会话页。
            "echo_user": True,
        }
    }
    _NS._RUNNING_TURNS[session_id] = turn_id
    try:
        await _run_stream(
            None, session["graph"], config, text, session, agent_id,
            user_extra_kwargs=extra_kwargs,
        )
    finally:
        if _NS._RUNNING_TURNS.get(session_id) == turn_id and session_id not in _NS._PENDING_RESUME:
            _NS._RUNNING_TURNS.pop(session_id, None)
        _NS._TURN_STOP.pop(session_id, None)
        if session_id not in _NS._PENDING_RESUME:
            try:
                await _NS.on_turn_settled(session_id, stop_evt, turn_id)
            except Exception:
                _NS._log.exception("subagent_settle_failed session=%s", session_id)


async def _inject_result(
    parent_id: str, text: str, child_id: str, extra_kwargs: dict | None = None
) -> None:
    """Channel pick (§5.5): running parent → steer stash; idle parent → new
    turn. Defense in depth for the terminal-parent rule above: a terminal
    parent is idle by definition, so without its own check the wake branch
    would open a turn on a stopped/failed/done session.

    ``extra_kwargs``（可空）随信封合并进持久化 HumanMessage 的
    additional_kwargs（ginno_subagent_result 戳之外再带溯源键——worker
    协议的 ginno_worker_msg 走这里）；子代理既有调用方不传，行为不变。"""
    pfound = _NS._find_meta(parent_id)
    if pfound and _NS._is_subagent(pfound[0]) and _NS._subagent_status(pfound[0]) in _NS.TERMINAL_STATUSES:
        _NS._log.warning(
            "subagent_inject_refused_terminal_parent parent=%s child=%s",
            parent_id, child_id,
        )
        return
    task = _NS._TURN_TASKS.get(parent_id)
    parent_live = parent_id in _NS._RUNNING_TURNS or (
        task is not None and not task.done()
    )
    if parent_live:
        _NS.steer_enqueue(
            parent_id,
            {
                # New id, never a reused turn id (checkpointer delta is id-keyed).
                "steer_id": f"subagent-{child_id}-{_NS.uuid.uuid4().hex[:8]}",
                "turn_id": _NS._RUNNING_TURNS.get(parent_id) or "",
                "text": text,
                "injected_at": _NS.time.time(),
                # Contract 3: the persisted HumanMessage carries
                # additional_kwargs["ginno_subagent_result"] = child_id
                # (merged by steer_messages alongside ginno_steer).
                "extra_kwargs": {"ginno_subagent_result": child_id, **(extra_kwargs or {})},
            },
        )
        _NS._log.info("subagent_result_steer parent=%s child=%s", parent_id, child_id)
    else:
        _queue_injection(parent_id, text, child_id, extra_kwargs)


def _queue_injection(
    parent_id: str, text: str, child_id: str, extra_kwargs: dict | None = None
) -> None:
    """Idle-parent result: coalesce (see the merged-injection note above)."""
    _NS._PENDING_INJECTIONS.setdefault(parent_id, []).append(
        {"text": text, "child_id": child_id, "extra": extra_kwargs or {}}
    )
    if parent_id in _NS._INJECTION_FLUSH_TASKS:
        return  # already scheduled — this entry rides the pending flush
    pfound = _NS._find_meta(parent_id)
    if not pfound:
        _NS._PENDING_INJECTIONS.pop(parent_id, None)
        return
    if parent_id in _NS._INJECTION_WAKING:
        # A flush's wake is lifting off (its lock section hasn't registered
        # _RUNNING_TURNS yet). Don't race it with a second immediate wake —
        # schedule the timer; by then the parent is live and the flush steers.
        _NS._INJECTION_FLUSH_TASKS[parent_id] = _NS.spawn_bg(
            _injection_flush_timer(parent_id)
        )
        return
    if not _NS._live_descendants(pfound[1], parent_id):
        _NS.spawn_bg(_flush_injections(parent_id))
        return
    _NS._INJECTION_FLUSH_TASKS[parent_id] = _NS.spawn_bg(_injection_flush_timer(parent_id))


async def _injection_flush_timer(parent_id: str) -> None:
    try:
        await _NS.asyncio.sleep(_NS.INJECTION_COALESCE_S)
    finally:
        _NS._INJECTION_FLUSH_TASKS.pop(parent_id, None)
    await _flush_injections(parent_id)


async def _flush_injections(parent_id: str) -> None:
    if parent_id in _NS._INJECTION_WAKING:
        if parent_id not in _NS._INJECTION_FLUSH_TASKS:
            _NS._INJECTION_FLUSH_TASKS[parent_id] = _NS.spawn_bg(
                _injection_flush_timer(parent_id)
            )
        return
    entries = _NS._PENDING_INJECTIONS.pop(parent_id, [])
    if not entries:
        return
    pfound = _NS._find_meta(parent_id)
    if not pfound:
        _NS._log.info("subagent_inject_dropped_no_meta parent=%s", parent_id)
        return
    meta = pfound[0]
    if _NS._is_subagent(meta) and _NS._subagent_status(meta) in _NS.TERMINAL_STATUSES:
        _NS._log.warning(
            "subagent_inject_refused_terminal_parent parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        return
    task = _NS._TURN_TASKS.get(parent_id)
    parent_live = parent_id in _NS._RUNNING_TURNS or (
        task is not None and not task.done()
    )
    if parent_live:
        # The parent went live while the queue waited (user spoke / an
        # earlier wake won): same shape as _inject_result's running branch,
        # one stash entry each — the superstep drain batches them.
        for e in entries:
            _NS.steer_enqueue(
                parent_id,
                {
                    "steer_id": f"subagent-{e['child_id']}-{_NS.uuid.uuid4().hex[:8]}",
                    "turn_id": _NS._RUNNING_TURNS.get(parent_id) or "",
                    "text": e["text"],
                    "injected_at": _NS.time.time(),
                    "extra_kwargs": {"ginno_subagent_result": e["child_id"], **(e.get("extra") or {})},
                },
            )
        _NS._log.info(
            "subagent_result_steer parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        return
    texts = [e["text"] for e in entries]
    ids = ",".join(e["child_id"] for e in entries)
    merged = texts[0] if len(texts) == 1 else (
        _NS.t(
            f"Below are the returns from {len(texts)} subagents "
            "(results and failure reports):\n\n",
            f"以下是 {len(texts)} 个子代理的回传（结果与失败报告）：\n\n",
        )
        + "\n\n".join(texts)
    )
    # 合并注入时各条目的溯源键取并集（键冲突以靠后的条目为准——实践中
    # 只有 worker 协议的 ginno_worker_msg 用到这里，冲突可忽略）。
    merged_extra: dict = {"ginno_subagent_result": ids}
    for e in entries:
        merged_extra.update(e.get("extra") or {})
    _NS._log.info(
        "subagent_result_wake parent=%s children=%s merged=%d",
        parent_id, ids, len(entries),
    )
    _NS._INJECTION_WAKING.add(parent_id)
    try:
        # NB: not named ``t`` — that would shadow the lang selector above.
        _wake_task = _NS.spawn_bg(
            _NS._wake_parent_turn(parent_id, merged, merged_extra)
        )

        def _wake_done(_f: Any, pid: str = parent_id) -> None:
            _NS._INJECTION_WAKING.discard(pid)

        _wake_task.add_done_callback(_wake_done)
    except Exception:
        _NS._INJECTION_WAKING.discard(parent_id)
        raise


async def _wake_parent_turn(
    parent_id: str,
    text: str,
    extra_kwargs: dict | None,
    steer_origin: str = "",
) -> str:
    """Open a NEW turn on an idle parent carrying the injected message.

    返回出口码（既有调用方忽略返回值；worker 协议的投递回执靠它）：
    ``ran`` / ``steered``（锁内发现目标忙，降级 steer 排队）/
    ``refused:<terminal|nonlive|stopped|no-session>``。"""
    from ..api.sessions import _ensure_session  # lazy: cycle

    # Never wake a terminal subagent (design §5.6): a stale wake racing a
    # cascade stop must not resurrect a stopped session to burn a turn.
    _wfound = _NS._find_meta(parent_id)
    if _wfound and _NS._is_subagent(_wfound[0]) and _NS._subagent_status(_wfound[0]) in _NS.TERMINAL_STATUSES:
        _NS._log.warning("subagent_wake_refused_terminal_parent parent=%s", parent_id)
        return "refused:terminal"
    parent = _NS._SESSIONS.get(parent_id) or _ensure_session(parent_id)
    if parent is None:
        _NS._log.warning("subagent_wake_no_parent parent=%s", parent_id)
        return "refused:no-session"
    async with _turn_lock_of(parent_id):
        # major-A: the pre-lock check raced every other finalizer — two children
        # of one waiting parent each judge the parent idle before the other's
        # wake flips anything, and the SECOND wake only reaches the lock after
        # the first wake's settle has already finalized the parent done. Re-read
        # the meta UNDER the lock and drop any delivery to a parent that is no
        # longer live (terminal, or a subagent status that is neither running
        # nor waiting): running it would burn a full ghost turn and re-inject
        # the same result into the grandparent.
        _lfound = _NS._find_meta(parent_id)
        if _lfound and _NS._is_subagent(_lfound[0]) and _NS._subagent_status(_lfound[0]) not in _NS.LIVE_STATUSES:
            _NS._log.warning(
                "subagent_wake_refused_nonlive_parent parent=%s status=%s",
                parent_id, _NS._subagent_status(_lfound[0]),
            )
            return "refused:nonlive"
        # major-B (same check-then-act root): a user stop that landed while
        # this wake queued on the lock must never be answered with a fresh
        # turn — it would run ahead of the stopped finalize and swallow it.
        _preset = _NS._TURN_STOP.get(parent_id)
        if _preset is not None and _preset.is_set():
            _NS._log.info("subagent_wake_refused_stopped_parent parent=%s", parent_id)
            _NS._TURN_STOP.pop(parent_id, None)
            return "refused:stopped"
        task = _NS._TURN_TASKS.get(parent_id)
        if parent_id in _NS._RUNNING_TURNS or parent_id in _NS._PENDING_RESUME or (
            task is not None and not task.done()
        ):
            # Raced busy: the steer channel still delivers it this turn.
            # steer_origin（worker 协议传 "worker"）让段末清场保留未吸收
            # 条目——与直接 steer 同一 eventual-delivery 待遇。
            _NS.steer_enqueue(
                parent_id,
                {
                    "steer_id": f"subagent-wake-{_NS.uuid.uuid4().hex[:8]}",
                    "turn_id": _NS._RUNNING_TURNS.get(parent_id) or "",
                    "text": text,
                    "injected_at": _NS.time.time(),
                    "origin": steer_origin or None,
                    "extra_kwargs": extra_kwargs,
                },
            )
            return "steered"
        # A waiting subagent waking up is running again (contract 6). Register a
        # provisional busy marker BEFORE the flip broadcast: between the checks
        # above and _run_managed_turn_locked's own registration sit the flip's
        # awaits (the WS push), and a user stop landing there read a session
        # that looked IDLE — the WS liveness gate set no event (stop swallowed,
        # the full wake turn ran against it) and a cascade direct-finalized the
        # subagent "stopped" out from under the wake, which then still opened a
        # ghost turn on the dead session. With the marker down, both stop paths
        # arm the event, and the re-check right after the flip refuses.
        _wake_tid = f"wake-{_NS.uuid.uuid4().hex[:8]}"
        _NS._RUNNING_TURNS[parent_id] = _wake_tid
        flipped = False
        try:
            found = _NS._find_meta(parent_id)
            if found and _NS._subagent_status(found[0]) == "waiting":
                sa = dict(found[0].get("subagent") or {})
                sa["status"] = "running"
                _NS._session_meta_patch(found[1], parent_id, {"subagent": sa})
                _NS._touch_in_memory(parent_id, {"subagent": sa})
                await _NS._push_both(
                    found[0].get("parent_session_id"),
                    parent_id,
                    "subagent.status",
                    {
                        "session_id": parent_id,
                        "parent_session_id": found[0].get("parent_session_id"),
                        "status": "running",
                    },
                )
                flipped = True
            stop_evt = _NS._TURN_STOP.setdefault(parent_id, _NS.asyncio.Event())
            if stop_evt.is_set():
                # major-B: set in the flip window above (cascade stop found the
                # existing entry) — same refusal as the pre-set check. A session
                # we JUST flipped to running must not stay stranded mid-state:
                # the stop that landed settles it "stopped" here.
                _NS._log.info("subagent_wake_refused_stopped_parent parent=%s", parent_id)
                _NS._TURN_STOP.pop(parent_id, None)
                if flipped:
                    await _NS.on_turn_stopped(parent_id)
                return "refused:stopped"
            try:
                await _run_managed_turn_locked(parent_id, text, extra_kwargs, stop_evt)
            except Exception:
                _NS._log.exception("subagent_wake_turn_failed parent=%s", parent_id)
        finally:
            if _NS._RUNNING_TURNS.get(parent_id) == _wake_tid:
                _NS._RUNNING_TURNS.pop(parent_id, None)
        return "ran"


async def _reevaluate_waiting_parent(parent_id: str | None) -> None:
    """Anti-starvation wake (§5.6): a waiting subagent with NO live descendant
    left gets one wrap-up message — no one else would inject into it again.

    Mixed terminal subtrees count (major-2): C1 done + C2 stopped leaves no one
    to wake the parent, because a stopped child injects nothing and the done
    child's own wake already happened while C2 was still running. The only
    skip is ALL-done/failed: every one of those finalizers injected its own
    result wake, so the parent is already covered."""
    if not parent_id:
        return
    found = _NS._find_meta(parent_id)
    if not found:
        return
    meta, slug = found
    if _NS._subagent_status(meta) != "waiting":
        return
    descendants = _NS._session_meta_descendants(slug, parent_id)
    if any(_NS._subagent_status(d) in _NS.LIVE_STATUSES for d in descendants):
        return
    if descendants and all(
        _NS._subagent_status(d) in ("done", "failed") for d in descendants
    ):
        return  # each finalizer already injected its own wake
    stopped = sum(1 for d in descendants if _NS._subagent_status(d) == "stopped")
    _NS._log.info(
        "subagent_children_all_settled parent=%s stopped=%d total=%d",
        parent_id, stopped, len(descendants),
    )
    wrap_up = (
        _NS.t(
            "All of your subtasks have been stopped by the user. Wrap up now: "
            "report what has been completed so far and the remaining risks.",
            "你的子任务均已被用户停止。请直接收尾：汇报目前已完成的部分与剩余风险。",
        )
        if not descendants or stopped == len(descendants)
        else _NS.t(
            "Some of your subtasks were stopped by the user and the rest are "
            "complete. Wrap up now: integrate the completed subtasks' results "
            "and explain the stopped parts.",
            "你的部分子任务已被用户停止，其余已完成。请直接收尾：整合已完成子任务的结果，并说明被停止的部分。",
        )
    )
    # Fold any queued result injections into this wrap-up wake: one turn, one
    # message (the merged-injection queue owns idle wakes for results).
    entries = _NS._PENDING_INJECTIONS.pop(parent_id, [])
    if entries:
        _t = _NS._INJECTION_FLUSH_TASKS.pop(parent_id, None)
        if _t is not None:
            _t.cancel()
        _NS._log.info(
            "subagent_inject_merged_into_wrapup parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        wrap_up = "\n\n".join([e["text"] for e in entries] + [wrap_up])
    _NS.spawn_bg(
        _NS._wake_parent_turn(
            parent_id,
            wrap_up,
            {"ginno_subagent_result": "stopped-children"},
        )
    )
