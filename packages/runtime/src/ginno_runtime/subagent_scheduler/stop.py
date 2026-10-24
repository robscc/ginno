"""Stop cascade + cascade delete + wait (contract 7, design §5.7/§5.8):
cooperative stop over the parent_session_id tree, delete-path settlement, and
the blocking wait behind wait_for_subagents."""

from __future__ import annotations

import asyncio
import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


async def _stop_parked_descendant(session_id: str, turn_id: str) -> None:
    """Heal + finalize one PARKED descendant (permission/ask_user interrupt) of
    a cascade stop. The same path the WS stop branch uses for a parked turn:
    heal the dangling checkpoint state, broadcast turn.stopped, drop the stash
    (result injections in it are reconciled first) and settle the subagent as
    "stopped" — otherwise the meta stays running forever, the sidebar shows a
    live dot and the checkpoint keeps an unresolved interrupt (major-4)."""
    from ..api.sessions import _ensure_session  # lazy: cycle
    from ..api.stream import _stop_parked_turn  # lazy: cycle

    session = _NS._SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        return
    await _stop_parked_turn(session, session_id, turn_id)


async def stop_subagent_tree(session_id: str, include_self: bool = True) -> list[str]:
    """Cooperatively stop the session + every descendant (contract 7, §5.6/5.7).

    Three descendant shapes, each settled explicitly so no meta is ever left
    running/waiting under a stopped ancestor:

    * live turn → set the cooperative event; the turn's own machinery
      finalizes "stopped" (the pre-arm discipline guarantees every live turn
      has an event);
    * parked at a permission/ask_user interrupt → no live task will ever
      observe an event: heal + finalize here (major-4);
    * waiting (or a stale running meta with no turn at all) → finalize
      "stopped" directly — a waiting subagent has no turn to signal, and
      leaving it would strand it ⏳/🟢 forever (major-3).

    Returns the ids a stop was signalled for (event set or parked-healed)."""
    stopped: list[str] = []
    for sid in _NS._tree_ids(session_id, include_self):
        found = _NS._find_meta(sid)
        live_sub = bool(
            found
            and _NS._is_subagent(found[0])
            and _NS._subagent_status(found[0]) not in _NS.TERMINAL_STATUSES
        )
        evt = _NS._TURN_STOP.get(sid)
        armed_fresh = False
        if evt is not None:
            evt.set()
            stopped.append(sid)
        elif live_sub:
            # No event yet: every LIVE turn pre-arms one, so this is a QUEUED
            # wake (or one inside its flip startup) that holds or will take the
            # session's turn lock — arm the event anyway so the wake's
            # under-lock check refuses instead of opening a fresh turn on a
            # session being stopped.
            _NS._TURN_STOP.setdefault(sid, asyncio.Event()).set()
            armed_fresh = True
        if sid in _NS._PENDING_RESUME:
            _NS._PENDING_RESUME.discard(sid)
            _NS._PENDING_KIND.pop(sid, None)
            stopped.append(sid)
            # The parked turn keeps a _RUNNING_TURNS entry (its resume never
            # came) — pop it here, or the cascade-delete busy wait spins on it.
            tid = _NS._RUNNING_TURNS.pop(sid, "")
            try:
                await _stop_parked_descendant(sid, tid)
            except Exception:
                _NS._log.exception("subagent_stop_parked_descendant_failed session=%s", sid)
            continue
        if armed_fresh and sid not in _NS._RUNNING_TURNS:
            task = _NS._TURN_TASKS.get(sid)
            if task is None or task.done():
                # Nothing live will ever observe the event we just armed (a
                # waiting session, or the queued-wake case above): settle it
                # stopped here, and drop the event so it cannot kill a future
                # legitimate turn. A session with a live/starting turn is left
                # to its own machinery via the event.
                _NS._TURN_STOP.pop(sid, None)
                try:
                    await _NS._finalize_subagent(found[1], found[0], "stopped")
                except Exception:
                    _NS._log.exception("subagent_stop_finalize_failed session=%s", sid)
    return stopped


async def stop_descendants(session_id: str) -> list[str]:
    """Cascade half of stop_subagent_tree (the session's own turn is handled by
    the WS stop branch that calls this)."""
    return await stop_subagent_tree(session_id, include_self=False)


async def wait_for_subagents(
    parent_session_id: str, ids: list[str] | None = None, timeout_s: int = 0
) -> str:
    """Block until the named (default: all DIRECT) children reach a terminal
    status; returns their status summaries as JSON. ``timeout_s > 0`` returns a
    marked partial at the deadline. Interruptible by the session's cooperative
    stop signal — the tool returns a ``[stopped]`` marker and the turn ends at
    the next chunk boundary, exactly like any other stopped turn."""
    slug = _NS._session_slug(parent_session_id) or "default"
    deadline = _NS.time.monotonic() + max(0, int(timeout_s or 0)) if (timeout_s or 0) > 0 else None

    def _snapshot() -> tuple[list[dict], list[str]]:
        rows = _NS.collect_subagent_rows(slug, parent_session_id, include_descendants=True)
        by_id = {r["session_id"]: r for r in rows}
        if ids:
            chosen = [by_id[i] for i in ids if i in by_id]
            missing = [i for i in ids if i not in by_id]
        else:
            chosen = [r for r in rows if r.get("parent_session_id") == parent_session_id]
            missing = []
        return chosen, missing

    chosen, missing = _snapshot()
    if ids and missing and not chosen:
        # Nothing resolvable: waiting forever would be a deadlock-shaped bug —
        # answer with the error instead.
        return _NS.t(
            f"[error] Subagent(s) not found: {', '.join(missing)}",
            f"[error] 未找到指定的 subagent：{', '.join(missing)}",
        )

    while True:
        chosen, missing = _snapshot()
        if not missing and (
            not chosen or all(r["status"] in _NS.TERMINAL_STATUSES for r in chosen)
        ):
            return _NS.json.dumps(
                {"partial": False, "subagents": _NS._wait_rows(chosen)}, ensure_ascii=False
            )
        evt = _NS._TURN_STOP.get(parent_session_id)
        if evt is not None and evt.is_set():
            return _NS.t(
                "[stopped] The user stopped the current turn; the wait was "
                "interrupted. Current child statuses:\n",
                "[stopped] 用户停止了当前回合，等待中断。当前子代状态：\n",
            ) + _NS.json.dumps(
                {"partial": True, "subagents": _NS._wait_rows(chosen)}, ensure_ascii=False
            )
        if deadline is not None and _NS.time.monotonic() >= deadline:
            return _NS.t(
                "(partial) Wait timed out; not all children have finished:\n",
                "(partial) 等待超时，子代尚未全部结束：\n",
            ) + _NS.json.dumps(
                {"partial": True, "subagents": _NS._wait_rows(chosen)}, ensure_ascii=False
            )
        await asyncio.sleep(0.2)


# --------------------------------------------------------------------------- #
# cascade delete (§5.8)
# --------------------------------------------------------------------------- #

async def finalize_for_delete(session_id: str) -> None:
    """Delete-path settlement (§5.8, major-5): the DELETE endpoint removes the
    meta directly, so the settle hook can never find it again (_find_meta →
    None → return) — a waiting ancestor of the removed session would stay
    ⏳ forever with no one left to wake it. Finalize a live subagent as
    "stopped" BEFORE its meta disappears, then re-evaluate every surviving
    waiting ancestor up the chain."""
    found = _NS._find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if _NS._is_subagent(meta) and _NS._subagent_status(meta) not in _NS.TERMINAL_STATUSES:
        # Stop a live turn so it unwinds instead of writing into a deleted
        # checkpoint; its settle hook no-ops (meta gone / already terminal).
        evt = _NS._TURN_STOP.get(session_id)
        if evt is not None:
            evt.set()
        try:
            await _NS._finalize_subagent(slug, meta, "stopped")
        except Exception:
            _NS._log.exception("subagent_delete_finalize_failed session=%s", session_id)
    seen: set[str] = {session_id}
    pid = meta.get("parent_session_id")
    while pid and pid not in seen:
        seen.add(pid)
        try:
            await _NS._reevaluate_waiting_parent(pid)
        except Exception:
            _NS._log.exception("subagent_delete_reevaluate_failed parent=%s", pid)
        pfound = _NS._find_meta(pid)
        pid = pfound[0].get("parent_session_id") if pfound else None


async def delete_subagent_tree(slug: str, session_id: str) -> list[str]:
    """Stop + remove every DESCENDANT of ``session_id`` (the caller removes the
    session itself). Order matters: cooperative stop → wait for the turn tasks
    to unwind → only then delete checkpoint files, so nothing writes back into
    a deleted file."""
    ids = [m["id"] for m in _NS._session_meta_descendants(slug, session_id)]
    if not ids and session_id not in _NS._SESSIONS:
        return []
    await stop_subagent_tree(session_id, include_self=True)
    deadline = _NS.time.monotonic() + 10.0
    while _NS.time.monotonic() < deadline:
        # A PARKED session (permission/ask_user interrupt) keeps a permanent
        # _RUNNING_TURNS entry with no live task behind it — the cascade stop
        # above already healed + popped it, but exclude pending-resume ids
        # explicitly so a straggler can never spin the wait to the 10s cap
        # (major-4).
        busy = [
            sid
            for sid in ([session_id] + ids)
            if sid not in _NS._PENDING_RESUME
            and (
                sid in _NS._RUNNING_TURNS
                or ((_NS._TURN_TASKS.get(sid) is not None) and not _NS._TURN_TASKS[sid].done())
            )
        ]
        if not busy:
            break
        await asyncio.sleep(0.1)
    for cid in ids:
        _NS._SESSIONS.pop(cid, None)
        _NS._PENDING_RESUME.discard(cid)
        _NS._PENDING_KIND.pop(cid, None)
        _NS._TURN_STOP.pop(cid, None)
        from ..server_shared import subagent_unlink_any

        subagent_unlink_any(cid)
        _NS._session_meta_remove(slug, cid)
        from ..checkpointer import remove_checkpoint_files

        remove_checkpoint_files(slug, cid)
        _NS._log.info("subagent_cascade_deleted parent=%s child=%s", session_id, cid)
    return ids
