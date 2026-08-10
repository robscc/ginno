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

# Sessions paused at a permission/version-propose interrupt awaiting a resume.
# Turn events broadcast to EVERY socket, so two open tabs both show the prompt;
# this flag lets the second permission_response be ignored instead of resuming
# an already-resumed graph.
_PENDING_RESUME: set[str] = set()

# Fire-and-forget background tasks (MCP lazy retry etc.). asyncio keeps only
# WEAK references to tasks, so an unreferenced create_task() can be garbage
# collected mid-flight; hold strong refs until done.
_BG_TASKS: set[Any] = set()


def spawn_bg(coro: Any) -> Any:
    """create_task with a strong reference kept until completion."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


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
