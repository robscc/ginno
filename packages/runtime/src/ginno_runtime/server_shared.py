"""Process-wide mutable state + event-push helpers shared by all API modules.

server.py used to hold these globals inline; they live here so the per-domain
router modules (api/*.py) can share one registry without importing server.py
(which would create an import cycle — server.py includes the routers).

Everything in this module is mutated in place (dicts/sets/locks) or is a
plain function, so re-exporting the names from server.py keeps existing
call sites and test references valid.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import paths

_log = logging.getLogger("ginno.turn")
_log.setLevel(logging.INFO)
_log.propagate = False


def _ensure_turn_log() -> None:
    """Attach a rotating file handler under the *current* paths.home()/logs.

    Done lazily (called per turn) because paths.home() may not be final at import
    time — e.g. tests redirect GINNO_HOME after importing this module. If the home
    moves, the stale handler is swapped so traces always land in the active home
    (the real ~/.ginno in production, the isolated tmp dir in tests)."""
    try:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path as _Path

        target = (paths.home() / "logs" / "sidecar.log").resolve()
        for h in list(_log.handlers):
            if isinstance(h, RotatingFileHandler):
                if _Path(getattr(h, "baseFilename", "")).resolve() == target:
                    return  # already pointing at the right file
                _log.removeHandler(h)
                h.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(target, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(fh)
    except Exception:  # never let logging setup break a turn
        pass


# Process-wide MCP registry — spawned at startup (lifespan) and swapped by
# /api/mcp/reload. Typed Any to keep this module import-light (the registry
# pulls in the mcp client stack).
_mcp: Any = None
# Hook dispatcher rebuilt from settings at startup.
_hooks: Any = None

# Session registry: holds the compiled graph + metadata (in-memory; the
# on-disk source of truth for the list is the per-slug session index).
_SESSIONS: dict[str, dict[str, Any]] = {}
# Per-session cumulative model usage (plan D2/D4). In-memory only — resets on
# runtime restart, matching the "this app session" meaning users expect.
_USAGE_BY_SESSION: dict[str, dict[str, int]] = {}

# Background workflow-run tasks (run_id -> asyncio.Task); kept alive + awaitable
# so the run trigger can be fire-and-forget in prod yet deterministic in tests.
_WF_RUN_TASKS: dict[str, Any] = {}

# Live session WebSockets (session_id -> [WebSocket]); used to push run.* events
# into the conversation that a run is bound to (design A: run 回到对话). Self-cleans
# on send failure (disconnected sockets are dropped). Since 2026-08-05 the
# per-turn stream also broadcasts through this registry (see _stream_graph):
# turn events are no longer tied to the one socket that sent the invoke, so a
# mid-turn reconnect keeps receiving the running stream.
_SESSION_WS: dict[str, list[Any]] = {}

# Sessions with a turn currently streaming (session_id -> turn_id). Answers the
# client's `turn_state` query after a reconnect so the UI can distinguish
# "stream will resume" from "turn is gone — reconcile from history".
_RUNNING_TURNS: dict[str, str] = {}

# Live RUN-scoped WebSockets (run_id -> [WebSocket]); design B P2. The Studio
# observer (and any headless-run watcher) subscribes by run_id — independent of
# which session the run is presented in, unlike _SESSION_WS. Self-cleans on send
# failure, same as the session registry. Every push that goes to the presenting
# session also goes here (the driver calls both), so a run always has a live
# channel even when present_in_session_id is None (todo-sync / headless).
_RUN_WS: dict[str, list[Any]] = {}


async def _push_run_event(run_id: str | None, event: str, data: dict) -> None:
    """Best-effort push of a WS event to every live run-scoped socket.

    Mirror of :func:`_push_session_event` for the run channel: broadcast to all
    sockets of the run, prune the ones that fail. A run with no subscribers is
    a no-op (the events.jsonl + run JSON remain the source of truth)."""
    if not run_id:
        return
    socks = _RUN_WS.get(run_id) or []
    alive: list[Any] = []
    for w in socks:
        if await _try_send(w, _ev(event, data)):
            alive.append(w)
    _RUN_WS[run_id] = alive

# Sessions paused at a permission/version-propose interrupt awaiting a resume.
# Turn events broadcast to EVERY socket, so two open tabs both show the prompt;
# this flag lets the second permission_response be ignored instead of resuming
# an already-resumed graph.
_PENDING_RESUME: set[str] = set()

# WHICH interrupt kind is parked for the session. The resume payload shape is
# kind-specific (permission/version-propose → {"decision": ...}, ask_user →
# {"kind": "user_answer", ...}), so a stale or duplicated client message must
# not resume with the wrong shape. NOT a replacement for _PENDING_RESUME —
# that set stays the single "a resume is in flight" guard; this only labels it.
_PENDING_KIND: dict[str, str] = {}

# Live turn tasks (session_id -> asyncio.Task for the invoke/resume job). The
# WS receive loop no longer awaits turns inline (it must stay free to accept a
# `stop` message mid-turn), so this registry answers "is a turn running here?"
# for the busy check and the stop handler. Identity-checked done callbacks pop.
_TURN_TASKS: dict[str, Any] = {}

# Cooperative stop signals (session_id -> asyncio.Event). Created by the WS
# loop BEFORE the turn task spawns (so the stop handler never races a task
# that exists but hasn't registered in _RUNNING_TURNS yet), consumed by
# _stream_graph's chunked_stream, popped in the turn's finally. NEVER left
# set for an idle session — a stale set event would kill the next turn.
_TURN_STOP: dict[str, Any] = {}

# Steering stash (docs/steering-design.md §3.2): messages the user sent while a
# turn was running, held here until the next `agent` superstep absorbs them.
# graph.agent_node drains this at its entry — that node sits immediately before
# every model request — so a stashed message reaches the model in the SAME turn,
# right after the tool results that were streaming when the user typed it. This
# is Claude Code's mid-turn "absorption" (interactive-mode.md §"When Claude Code
# sends what you queued").
#
# Lifetime = ONE turn segment (invoke or resume): that segment's finally clears
# it, so an entry which missed its absorption window can never leak into a later
# turn. Dropping it is safe because the CLIENT owns the queue: it re-sends any
# entry it never saw acknowledged (as a normal `invoke` when the turn ended, or
# as `steer` again when it is about to resume a turn parked at an interrupt).
# steer_enqueue REPLACES by steer_id, so such a re-send can never duplicate the
# message.
_STEER_STASH: dict[str, list[dict]] = {}


def steer_enqueue(session_id: str, entry: dict) -> None:
    """Stash one steered message, keyed by steer_id (idempotent re-send)."""
    if not session_id:
        return
    lst = [
        e
        for e in _STEER_STASH.get(session_id, [])
        if e.get("steer_id") != entry.get("steer_id")
    ]
    lst.append(entry)
    _STEER_STASH[session_id] = lst


def steer_drain(session_id: str) -> list[dict]:
    """Pop every steered message awaiting absorption (graph.agent_node entry)."""
    return _STEER_STASH.pop(session_id, None) or []


def steer_clear(session_id: str) -> None:
    """Drop the stash at the end of a turn segment (see the lifetime note above)."""
    _STEER_STASH.pop(session_id, None)


# Drained by an agent superstep but not yet COMMITTED with its update
# (session_id -> entries). The window is the model call that is reading them:
# the ack is sent at drain time so the client can place the transcript band at
# the injection point (§3.2) — which means an ack no longer implies "durably in
# state" on its own, and this list is what makes it true again:
#   * the superstep's commit clears it (stream.py, updates/agent branch);
#   * a turn stopped inside that window commits them from _heal_interrupted_turn,
#     so the band the user already saw stays in the transcript;
#   * a parked exit puts them back in the stash for the resumed segment.
_STEER_INFLIGHT: dict[str, list[dict]] = {}


def steer_mark_inflight(session_id: str, entries: list[dict]) -> None:
    if session_id and entries:
        _STEER_INFLIGHT[session_id] = [*(_STEER_INFLIGHT.get(session_id) or []), *entries]


def steer_take_inflight(session_id: str) -> list[dict]:
    """Pop the uncommitted drain (heal path: commit these)."""
    return _STEER_INFLIGHT.pop(session_id, None) or []


def steer_clear_inflight(session_id: str) -> None:
    """Committed (or otherwise resolved): drop the uncommitted-drain record."""
    _STEER_INFLIGHT.pop(session_id, None)


def steer_restash_inflight(session_id: str) -> None:
    """Put an uncommitted drain back for the resumed segment to absorb again."""
    for entry in _STEER_INFLIGHT.pop(session_id, None) or []:
        steer_enqueue(session_id, entry)

# Fire-and-forget background tasks (MCP lazy retry etc.). asyncio keeps only
# WEAK references to tasks, so an unreferenced create_task() can be garbage
# collected mid-flight; hold strong refs until done.
_BG_TASKS: set[Any] = set()

# Main event loop, recorded in server.lifespan so sync (threadpool) handlers can
# still schedule coroutines (WS broadcasts) onto it.
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def spawn_bg(coro: Any) -> Any:
    """Schedule a coroutine, keeping a strong reference until completion.

    Works both from the event loop (create_task) and from threadpool workers
    (run_coroutine_threadsafe onto the recorded main loop) — sync REST handlers
    call this via _broadcast and must not raise "no running event loop"."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        t = loop.create_task(coro)
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)
        return t
    if _MAIN_LOOP is not None and not _MAIN_LOOP.is_closed():
        fut = asyncio.run_coroutine_threadsafe(coro, _MAIN_LOOP)
        _BG_TASKS.add(fut)
        fut.add_done_callback(_BG_TASKS.discard)
        return fut
    # No loop anywhere (shutdown edge): drop the coroutine rather than crash.
    coro.close()
    return None


# One frame may sit in a stuck/suspended client's buffer; never let it stall
# delivery to the session's OTHER sockets (or the turn loop itself). A client
# that can't keep up is pruned here and recovers via its reconnect path.
_WS_SEND_TIMEOUT_S = 5.0

# One asyncio lock per session serializing turn starts (goal continuation
# driver vs. user turns); one driver task per session with an active goal.
_TURN_LOCKS: dict[str, asyncio.Lock] = {}
_GOAL_DRIVERS: dict[str, asyncio.Task] = {}


def _ev(event: str, data: dict, turn_id: str | None = None) -> str:
    if turn_id:
        data = {"turn_id": turn_id, **data}
    return json.dumps({"event": event, **data}, ensure_ascii=False, default=str)


async def _try_send(w: Any, data: str) -> bool:
    try:
        await asyncio.wait_for(w.send_text(data), timeout=_WS_SEND_TIMEOUT_S)
        return True
    except Exception:
        return False


async def _push_session_event(
    session_id: str | None, event: str, data: dict, turn_id: str | None = None
) -> None:
    """Best-effort push of a WS event to every live socket of ``session_id``.

    Broadcast (not single-socket) by design: headless turns (goal
    continuation) have no invoking socket, and user turns must survive
    reconnects mid-stream."""
    if not session_id:
        return
    socks = _SESSION_WS.get(session_id) or []
    alive: list[Any] = []
    for w in socks:
        if await _try_send(w, _ev(event, data, turn_id)):
            alive.append(w)
    _SESSION_WS[session_id] = alive


async def _push_global_event(event: str, data: dict) -> None:
    """Push an event to every live socket of EVERY session.

    For global state changes (skills live in ~/.ginno/skills, shared by all
    sessions) — e.g. ``skills.changed`` so each open chat's slash menu and
    the WorldState-aware UI reload without a manual refresh.
    """
    for sid in list(_SESSION_WS.keys()):
        await _push_session_event(sid, event, data)


def _turn_lock(session_id: str) -> asyncio.Lock:
    lk = _TURN_LOCKS.get(session_id)
    if lk is None:
        lk = asyncio.Lock()
        _TURN_LOCKS[session_id] = lk
    return lk
