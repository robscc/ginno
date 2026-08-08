"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

Streams LangGraph events over WebSocket: token deltas, tool start/end,
permission requests (HITL), and final message boundaries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel

from . import agents as agents_reg
from . import files as files_mod
from . import paths
from . import providers as prov_mod
from .agents.memory import ensure_agent_memory
from .checkpointer import ABANDONED_TURNS, FileCheckpointer
from . import commands as _commands
from .graph import BLOCK_PREFIX, build_all_tools, build_graph, build_turn_context
from . import usage_store
from .usage import add_usage, cache_hit_ratio, empty_usage, extract_usage
from .world_state import (
    TURN_CONTEXT_PREFIX,
    UPDATE_MSG_PREFIX,
    ALL_CONTEXT_PREFIXES,
    GOAL_CONTEXT_PREFIX,
    SessionCtx,
    context_settings,
    sync_world_state,
)
from .goals.templates import context_row_text as goal_context_row

# Bullets a world-state update message can start with when it was checkpointed
# by a build that dropped the machine prefix — healed into context rows too.
LEGACY_WS_UPDATE_MARKERS = (
    "- 你在当前角色下的可用工具数量变化",
    "- MCP 工具已更新",
    "- Skills 已更新",
    "Skills 已更新",
)
from .tools.render_tools import RENDER_TOOL_NAMES
from .hooks.dispatcher import HookDispatcher
from .models import build_model
from .mcp.registry import MCPRegistry
from .skills.loader import SkillLoader
from .todos import store as todo_store
from .todos import providers as todo_providers
from .todos import sync_ledger
from . import artifacts as art_store
from . import workflows as wf_store
from .goals import store as goal_store
from .goals import events as goal_events
from .goals import templates as goal_templates
from .workflows import events as wf_events
from .workflows import dsl as wf_dsl
from .workflows import store as wf_storemod
from .tools.artifact_tools import ARTIFACT_TOOL_NAMES
from .tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES

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


# Process-wide MCP registry — spawned at startup.
_mcp: MCPRegistry | None = None
_hooks: HookDispatcher | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp, _hooks
    paths.ensure_layout()
    # Attach the trace-file handler up front so pre-turn lifecycle lines
    # (session_create / ws_open / ws_close) land in the log, not just turn lines.
    _ensure_turn_log()
    # Drop usage logs past the retention window (usage-stats-design.md §4.3).
    # Best-effort and cheap (a directory glob); never blocks startup on failure.
    try:
        usage_store.cleanup()
    except Exception:
        log.exception("usage cleanup failed (continuing)")
    # Best-effort, idempotent move of legacy session files into their per-session
    # dirs. Runs before `yield`, so nothing (uploads/previews/watchers) can race
    # it. Must never block startup on failure.
    try:
        from . import migration as _migration

        _migration.migrate_session_files()
    except Exception:
        log.exception("session-files migration failed (continuing)")
    _hooks = HookDispatcher.from_settings()
    todo_store.ensure_seeded()
    agents_reg.ensure_todo_tools()
    agents_reg.ensure_research_discipline()
    agents_reg.ensure_goal_tools()
    wf_store.ensure_seeded()
    # Heal session metas frozen with a provider/model by older builds (or a
    # config edited outside the UI) so topbar + rebuilt graphs use current config.
    _refresh_session_metas()
    _mcp = MCPRegistry()
    _mcp.load()
    # Do NOT block port bind on MCP connections. A slow/hung MCP server can
    # take up to its per-server timeout, and the HTTP/WS server (and thus the
    # UI) must come up regardless. Connect in the background; a session created
    # before connections finish simply starts without those tools (the
    # /api/mcp/reload endpoint or a new session picks them up once ready).
    mcp_connect_task = asyncio.create_task(_connect_mcp_background())
    try:
        yield
    finally:
        if not mcp_connect_task.done():
            mcp_connect_task.cancel()
        if _mcp:
            await _mcp.close_all()


async def _connect_mcp_background() -> None:
    try:
        await _mcp.connect_all()
    except Exception:
        log.exception("background MCP connect_all failed")


app = FastAPI(title="Ginno Runtime", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- serve the web UI from the sidecar (same origin as the API) ----
# In the packaged app the Tauri webview loads http://127.0.0.1:8787 directly,
# which avoids the tauri:// -> http cross-origin / mixed-content block and the
# startup race. The Next static export is bundled into the binary (web_out/) by
# PyInstaller; in dev we fall back to the repo's apps/web/out.
import sys as _sys
from pathlib import Path as _Path


def _web_out_dir() -> _Path | None:
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        p = _Path(meipass) / "web_out"
    else:
        p = _Path(__file__).resolve().parents[4] / "apps" / "web" / "out"
    return p if p.exists() else None


WEB_OUT = _web_out_dir()

if WEB_OUT is not None and (WEB_OUT / "_next").exists():
    from starlette.staticfiles import StaticFiles as _StaticFiles

    app.mount("/_next", _StaticFiles(directory=str(WEB_OUT / "_next")), name="next-static")


class CreateSessionRequest(BaseModel):
    project_slug: str
    workspace: str
    agent_id: str | None = None
    title: str | None = None
    icon: str | None = None
    provider: str | None = None
    model: str | None = None
    # legacy aliases
    model_provider: str | None = None
    model_name: str | None = None


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


# One frame may sit in a stuck/suspended client's buffer; never let it stall
# delivery to the session's OTHER sockets (or the turn loop itself). A client
# that can't keep up is pruned here and recovers via its reconnect path.
_WS_SEND_TIMEOUT_S = 5.0


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


# ---- Goal continuation driver (goal-design.md §4.3.3) ---------------------
# One asyncio task per session with an active goal. After every turn ends and
# the session goes idle, it injects a continuation message and starts the next
# turn HEADLESSLY (no client socket required — the user may have closed the
# window). Guards: user turns always win (turn lock + idle waits), pending
# permission interrupts stall continuation, a goal does not follow an agent
# switch (auto-pause). There is deliberately NO turn-count cap: context size
# is managed by the existing auto-compaction (E3).

GOAL_GRACE_S = 3.0  # pause between turns so the user can interject

_TURN_LOCKS: dict[str, asyncio.Lock] = {}
_GOAL_DRIVERS: dict[str, asyncio.Task] = {}


def _turn_lock(session_id: str) -> asyncio.Lock:
    lk = _TURN_LOCKS.get(session_id)
    if lk is None:
        lk = asyncio.Lock()
        _TURN_LOCKS[session_id] = lk
    return lk


async def _emit_goal_event(
    slug: str, session_id: str, goal: dict | None, turn_id: str | None = None
) -> None:
    if goal is None:
        await _push_session_event(session_id, "goal.cleared", {})
    else:
        await _push_session_event(session_id, "goal.updated", {"goal": goal}, turn_id)


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
    await _run_stream(None, session["graph"], config, text, session, agent_id)


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
            slug = session["project_slug"]
            goal = goal_store.get_goal(slug, session_id)
            if not goal or goal.get("status") != goal_store.STATUS_ACTIVE:
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


def _session_meta_list(slug: str) -> list[dict]:
    p = paths.session_index_path(slug)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def _session_meta_upsert(slug: str, entry: dict) -> None:
    items = [m for m in _session_meta_list(slug) if m.get("id") != entry["id"]]
    items.insert(0, entry)
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )


def _session_meta_patch(slug: str, session_id: str, patch: dict) -> dict | None:
    items = _session_meta_list(slug)
    target = None
    for m in items:
        if m.get("id") == session_id:
            m.update({k: v for k, v in patch.items() if v is not None})
            m["updated"] = time.time()
            target = m
    if target is None:
        return None
    paths.session_index_path(slug).write_text(
        json.dumps(items, indent=2, ensure_ascii=False)
    )
    return target


def _session_meta_remove(slug: str, session_id: str) -> bool:
    items = _session_meta_list(slug)
    kept = [m for m in items if m.get("id") != session_id]
    if len(kept) == len(items):
        return False
    paths.session_index_path(slug).write_text(
        json.dumps(kept, indent=2, ensure_ascii=False)
    )
    return True


def _agent_lookup(agent_id: str | None):
    return agents_reg.get_agent(agent_id) if agent_id else None


def _resolve_provider_model(req: CreateSessionRequest) -> tuple[str, str, str | None]:
    agent = _agent_lookup(req.agent_id)
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    # Prefer an *enabled* provider. The seed agents default to provider "custom",
    # which is disabled until configured; that must NOT block session creation —
    # fall through to the enabled global default so "enable a provider and use it"
    # just works without editing every agent.
    candidates = [
        req.provider,
        req.model_provider,
        agent.provider if agent else None,
        prov_mod.get_default_provider(),
    ]
    provider = next((c for c in candidates if _enabled(c)), None) or prov_mod.get_default_provider()
    model = (
        req.model
        or req.model_name
        or (agent.model if agent and agent.model else None)
        or prov_mod.model_for_provider(providers, provider)
    )
    return provider, model, (agent.id if agent else req.agent_id)


def _default_title(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return f"{a.name} session" if a else "New Session"


def _agent_icon(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return a.icon if a else "message-square"


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@app.get("/api/sessions/{session_id}/usage")
async def get_session_usage(session_id: str) -> dict:
    """Per-session cumulative model usage (the TopBar counter). The live
    `usage` WS event only fires on turns; this lets the UI show a session's
    accumulated stats immediately after a session switch.

    Source order (usage-stats-design.md §5): the persistent usage log first —
    it survives runtime restarts, so a session's total stays truthful across
    restarts — falling back to the in-memory accumulator when nothing was
    logged yet. Response shape is unchanged."""
    logged = usage_store.session_totals(session_id)
    if logged:
        return {"ok": True, "usage": logged}
    acc = _USAGE_BY_SESSION.get(session_id)
    if not acc:
        return {"ok": True, "usage": None}
    return {"ok": True, "usage": {**acc, "cache_hit_ratio": cache_hit_ratio(acc)}}


# ---- usage telemetry (usage-stats-design.md §5) ----------------------------


def _usage_session_display(sid: str) -> dict:
    """Join display meta for a usage row. Deleted sessions keep their usage
    (billing-style data) under a placeholder title (design §3.3)."""
    s = _SESSIONS.get(sid)
    if s:
        return {
            "title": s.get("title") or "",
            "icon": s.get("icon") or "message-square",
            "agent_id": s.get("agent_id"),
            "provider": s.get("model_provider") or "",
            "model": s.get("model_name") or "",
            "deleted": False,
        }
    found = _find_meta(sid)
    if found:
        m, _slug = found
        return {
            "title": m.get("title") or "",
            "icon": m.get("icon") or "message-square",
            "agent_id": m.get("agent_id"),
            "provider": m.get("provider") or "",
            "model": m.get("model") or "",
            "deleted": False,
        }
    return {
        "title": f"(已删除) {sid[:6]}",
        "icon": "message-square",
        "agent_id": None,
        "provider": "",
        "model": "",
        "deleted": True,
    }


@app.get("/api/usage/overview")
async def usage_overview(days: int = 30) -> dict:
    """KPI + daily series + provider/model breakdown for the trailing window
    (design §5). Window is clamped to [1, retention]."""
    days = max(1, min(int(days), usage_store.RETENTION_DAYS))
    return {"ok": True, **usage_store.aggregate_overview(days)}


@app.get("/api/usage/hourly")
async def usage_hourly(date: str | None = None) -> dict:
    """24-hour distribution for one day (defaults to today, local time)."""
    return {"ok": True, **usage_store.aggregate_hourly(date)}


@app.get("/api/usage/sessions")
async def usage_sessions(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    sort: str = "total",
    limit: int = 200,
) -> dict:
    """Per-session aggregates over [from, to] (defaults: full retention window
    ending today), joined with display meta."""
    rows = usage_store.aggregate_sessions(from_, to, sort=sort, limit=limit)
    for r in rows:
        sid = r.get("session_id")
        r.update(_usage_session_display(sid) if sid else {
            "title": "(后台/系统)", "icon": "cpu", "agent_id": None,
            "provider": "", "model": "", "deleted": False,
        })
    return {"ok": True, "sessions": rows}


@app.get("/api/usage/sessions/{session_id}")
async def usage_session_detail(
    session_id: str,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    """One session's aggregate within [from, to] (defaults: full retention)."""
    totals = usage_store.session_totals(session_id, from_, to)
    if totals is None:
        return {"ok": True, "usage": None}
    return {"ok": True, "usage": totals, **_usage_session_display(session_id)}


@app.get("/api/usage/requests")
async def usage_requests(
    date: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Request log for one day with filters + pagination (design §3.4)."""
    res = usage_store.query_requests(
        date_str=date, provider=provider, model=model, source=source,
        session_id=session_id, page=page, page_size=page_size,
    )
    return {"ok": True, **res}


# ---- session goal (goal-design.md §4.4) -----------------------------------

def _goal_slug(session_id: str) -> str | None:
    """Resolve the project slug for a session without building its graph."""
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None


def _goal_agent(session_id: str, slug: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s.get("agent_id")
    meta, _ = _find_meta(session_id) or ({}, slug)
    return (meta or {}).get("agent_id")


@app.get("/api/sessions/{session_id}/goal")
async def get_session_goal(session_id: str) -> dict:
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    return {"ok": True, "goal": goal_store.get_goal(slug, session_id)}


@app.put("/api/sessions/{session_id}/goal")
async def set_session_goal(session_id: str, req: dict) -> dict:
    """Create / replace the objective and/or change status.

    body: {objective?, status?, confirm?}
    * objective + existing UNFINISHED goal + no confirm → 409 needs_confirm.
    * status accepts only user actions: "active" (resume) | "paused".
    """
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
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


@app.delete("/api/sessions/{session_id}/goal")
async def clear_session_goal(session_id: str) -> dict:
    slug = _goal_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    cleared = goal_store.clear_goal(slug, session_id)
    if cleared:
        _stop_goal_driver(session_id)
        await _emit_goal_event(slug, session_id, None)
    return {"ok": True, "cleared": cleared}


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    provider, model_name, agent_id = _resolve_provider_model(req)
    try:
        model = build_model(provider, model_name)
    except ValueError as e:
        return {"error": str(e), "ok": False}

    mcp_tools = _mcp.all_langchain_tools() if _mcp else []
    session_id = uuid.uuid4().hex
    # Every session gets its own files directory, created now and PRESERVED on
    # delete. It supersedes the client-supplied `workspace` (a shared, non-
    # session-scoped path) as the authoritative home for this session's files.
    session_dir = paths.session_files_dir(req.project_slug, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(session_dir)
    all_tools = build_all_tools(
        mcp_tools, workspace=workspace, project_slug=req.project_slug, session_id=session_id
    )
    graph = build_graph(
        model=model,
        project_slug=req.project_slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=_hooks,
        all_tools=all_tools,
    )
    ag = _agent_lookup(agent_id)
    if ag:
        ensure_agent_memory(ag.id, ag.name)
    title = req.title or _default_title(agent_id)
    title_auto = not bool(req.title)  # auto title follows the active agent
    icon = req.icon or _agent_icon(agent_id)
    meta = {
        "id": session_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "agent_id": agent_id,
        "provider": provider,
        "model": model_name,
        "workspace": workspace,
        "created": time.time(),
        "updated": time.time(),
    }
    _session_meta_upsert(req.project_slug, meta)
    _log.info(
        "session_create session=%s agent=%s provider=%s model=%s title=%r",
        session_id,
        agent_id,
        provider,
        model_name,
        title,
    )
    _SESSIONS[session_id] = {
        "session_id": session_id,
        "project_slug": req.project_slug,
        "workspace": workspace,
        "agent_id": agent_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "model_provider": provider,
        "model_name": model_name,
        "graph": graph,
        # WorldState inputs (plan C1): the model for compaction summaries and
        # tool-name rosters for the agent/mcp sections' snapshots.
        "model": model,
        "all_tool_names": [t.name for t in all_tools],
        "mcp_tool_names": [t.name for t in mcp_tools],
    }
    # return the meta shape (with `id`) so the frontend SessionMeta matches
    return {**meta, "ok": True}


@app.get("/api/sessions")
async def list_sessions(project_slug: str | None = None) -> list[dict]:
    slug = project_slug or "default"
    on_disk = _session_meta_list(slug)
    if on_disk:
        return on_disk
    return [
        {k: v for k, v in s.items() if k != "graph"}
        for s in _SESSIONS.values()
        if s.get("project_slug") == slug
    ]


def _resolve_session_meta(session_id: str) -> dict | None:
    """Find a session's meta (with project_slug/workspace) in memory or on disk."""
    s = _SESSIONS.get(session_id)
    if s:
        return s
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m
    return None


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict | None:
    m = _resolve_session_meta(session_id)
    if m is None:
        return None
    return {k: v for k, v in m.items() if k != "graph"}


class PatchSessionRequest(BaseModel):
    title: str | None = None
    icon: str | None = None
    agent_id: str | None = None


@app.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: PatchSessionRequest) -> dict:
    s = _SESSIONS.get(session_id)
    slug = s["project_slug"] if s else "default"
    patch = req.model_dump()

    # Inspect the stored meta to honour the title_auto flag: an auto-generated
    # title ("X session") follows the active agent; a manually-set title sticks.
    cur = next((m for m in _session_meta_list(slug) if m.get("id") == session_id), None)
    title_auto = (cur or {}).get("title_auto", True)
    if patch.get("title") is not None:
        title_auto = False  # explicit rename → stop auto-following
    if patch.get("agent_id") is not None and title_auto:
        ag = _agent_lookup(patch["agent_id"])
        if ag:
            patch["title"] = f"{ag.name} session"
    patch["title_auto"] = title_auto

    updated = _session_meta_patch(slug, session_id, patch)
    if s:
        for k, v in patch.items():
            if v is not None:
                s[k] = v
        s["title_auto"] = title_auto
    return {
        "ok": True,
        "session": updated or (s and {k: v for k, v in s.items() if k != "graph"}),
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """Delete a session: its index entry, on-disk checkpoint history, and any
    in-memory graph cache. Returns ok=True even if the id was already gone.

    The session's files directory (`sessions/<session_id>/`) is intentionally
    PRESERVED — only the conversation (checkpoint + index row) is removed. The
    files stay browsable/cleanable via Settings → 会话文件. Note the checkpoint
    is the *file* `sessions/<session_id>.json`; the preserved dir is
    `sessions/<session_id>/` — never glob `<session_id>*` here.
    """
    s = _SESSIONS.pop(session_id, None)
    slug = s["project_slug"] if s else None
    removed = False
    # find the slug from the on-disk index if not known from memory
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        cand = slug_dir.parent.parent.name
        if _session_meta_remove(cand, session_id):
            removed = True
            slug = slug or cand
    # drop the checkpoint file (the full conversation history)
    files_dir = None
    if slug:
        cp = paths.project_sessions_dir(slug) / f"{session_id}.json"
        if cp.exists():
            try:
                cp.unlink()
                removed = True
            except OSError:
                pass
        # preserved files dir (may not exist for legacy sessions)
        fd = paths.session_files_dir(slug, session_id)
        if fd.is_dir():
            files_dir = str(fd)
        # cascade: drop the session's goal (goal-design.md §4.1) and stop any
        # continuation driver still looping for it.
        try:
            goal_store.clear_goal(slug, session_id)
        except Exception:
            _log.exception("goal_cascade_delete_failed session=%s", session_id)
        _stop_goal_driver(session_id)
    return {"ok": True, "removed": removed, "files_dir": files_dir}


# ---- session history (rebuild the chat UI's block layout from checkpoints) ----
def _image_block_url(b: dict) -> str | None:
    """Normalize a provider image block (OpenAI ``image_url`` / Anthropic
    ``image``) to a displayable URL (data URL for base64 sources)."""
    if b.get("type") == "image_url":
        iu = b.get("image_url") or {}
        u = iu.get("url") if isinstance(iu, dict) else None
        return u or None
    src = b.get("source") or {}
    if isinstance(src, dict):
        if src.get("type") == "url":
            return src.get("url") or None
        if src.get("data"):
            return f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    return None


def _content_ui_blocks(content: Any) -> list[dict]:
    """Message content (str or multimodal list) -> UI text/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                if b.strip():
                    blocks.append({"kind": "text", "text": b})
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    t = b.get("text") or ""
                    if t.strip():
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
    return blocks


def _tool_content_str(content: Any) -> str:
    """ToolMessage.content (str or list of provider blocks) -> plain text for
    the UI tool bubble. Image parts become an ``[image]`` marker."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    parts.append(b.get("text") or "")
                elif bt in ("image", "image_url"):
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False, default=str))
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


# Live WS tool outputs are capped to keep frames small; the history endpoint
# returns the full untruncated result, so expanding a bubble after reload can
# show more than what streamed live.
TOOL_OUTPUT_WS_LIMIT = 4000


def _truncate_for_ws(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_WS_LIMIT:
        return text
    return text[:TOOL_OUTPUT_WS_LIMIT] + f"\n…（已截断，完整 {len(text)} 字符）"


def _ai_content_blocks(content: Any) -> list[dict]:
    """AIMessage.content (str or list of provider blocks) -> UI text/thinking/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content:
            blocks.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                bt = b.get("type")
                if bt == "thinking":
                    t = b.get("thinking") or b.get("text") or ""
                    if t:
                        blocks.append({"kind": "thinking", "text": t})
                elif bt == "text":
                    t = b.get("text") or ""
                    if t:
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
            elif isinstance(b, str) and b:
                blocks.append({"kind": "text", "text": b})
    return blocks


def _run_id_in(text: str) -> str | None:
    m = re.search(r"run_id=([0-9a-f]{6,})", text or "")
    return m.group(1) if m else None


def _messages_to_ui(
    messages: list[Any], agent_id: str | None, attached_files: list[dict] | None = None
) -> list[dict]:
    """Convert stored LangChain messages into the chat UI's {role, blocks} shape.

    Consecutive assistant steps between two human messages are merged into a
    single assistant bubble, matching how a live turn renders (one bubble/turn).
    Special tools (render_widget/attach_ref/workflow_*) reproduce their visual
    blocks; ordinary tools fold their ToolMessage result into a tool block.
    """
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            results[getattr(m, "tool_call_id", None)] = _tool_content_str(getattr(m, "content", ""))

    ui: list[dict] = []
    acc: list[dict] | None = None
    acc_id: str | None = None

    def flush_assistant() -> None:
        nonlocal acc, acc_id
        if acc:
            ui.append({"id": acc_id, "role": "assistant", "agentId": agent_id, "blocks": acc})
        acc = None
        acc_id = None

    for m in messages:
        if isinstance(m, HumanMessage):
            content_raw = getattr(m, "content", "")
            # WorldState scaffolding messages (plan C2/E3/E4/B1): render the
            # user-facing ones as centered "context" rows (chips in the
            # transcript); hide the per-turn context bundle entirely — it is
            # model scaffolding, not conversation.
            if isinstance(content_raw, str) and (
                content_raw.startswith(ALL_CONTEXT_PREFIXES)
                or content_raw.startswith(LEGACY_WS_UPDATE_MARKERS)
            ):
                if content_raw.startswith(TURN_CONTEXT_PREFIX):
                    continue
                flush_assistant()
                # Goal steering messages (continuation / objective-updated) fold
                # into a SHORT centered row — the full prompt is model
                # scaffolding, not conversation (goal-design.md §4.3.2).
                if content_raw.startswith(GOAL_CONTEXT_PREFIX):
                    display = goal_context_row(content_raw)
                else:
                    # The update prefix is a machine marker — never show it.
                    display = content_raw
                    if display.startswith(UPDATE_MSG_PREFIX):
                        display = display[len(UPDATE_MSG_PREFIX):].lstrip("\n")
                ui.append(
                    {
                        "id": getattr(m, "id", None),
                        "role": "system",
                        "blocks": [{"kind": "context", "text": display}],
                    }
                )
                continue
            flush_assistant()
            blocks = _content_ui_blocks(content_raw)
            if attached_files and not ui:
                # first user bubble carries the turn's file chips
                file_blocks = [
                    {
                        "kind": "file",
                        "fileId": f.get("id"),
                        "name": f.get("name"),
                        "path": f.get("path"),
                        "fileKind": f.get("kind"),
                    }
                    for f in attached_files
                ]
                blocks = file_blocks + blocks
            if blocks:
                ui.append({"id": getattr(m, "id", None), "role": "user", "blocks": blocks, "turnId": getattr(m, "id", None)})
        elif isinstance(m, AIMessage):
            if acc is None:
                acc = []
                acc_id = getattr(m, "id", None)
            step = list(_ai_content_blocks(getattr(m, "content", "")))
            rk = (getattr(m, "additional_kwargs", None) or {}).get("reasoning_content")
            if rk:
                step.insert(0, {"kind": "thinking", "text": rk})
            for tc in getattr(m, "tool_calls", None) or []:
                nm = tc.get("name")
                args = tc.get("args") or {}
                tid = tc.get("id")
                res = results.get(tid, "")
                if nm == "render_widget":
                    step.append({"kind": "widget", "widgetKind": args.get("kind", "widget"), "data": args.get("data")})
                elif nm == "attach_ref":
                    step.append({
                        "kind": "ref",
                        "refKind": args.get("kind", "file"),
                        "name": args.get("name", ""),
                        "refId": args.get("ref_id", ""),
                    })
                elif nm in WORKFLOW_TOOL_NAMES:
                    rid = _run_id_in(res)
                    run = (RUN_CACHE.get(rid) if rid else None) or (wf_store.get_run(rid) if rid else None)
                    if run:
                        step.append({"kind": "workflow", "run": run})
                    else:
                        step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
                elif nm in ARTIFACT_TOOL_NAMES or nm in RENDER_TOOL_NAMES:
                    pass  # silent / already handled above
                else:
                    step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
            acc.extend(step)
        # ToolMessage: folded into the tool blocks above
    flush_assistant()
    return ui


def _session_slug(session_id: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None


@app.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str) -> dict:
    """Return the persisted chat history for a session, in the UI block format."""
    slug = _session_slug(session_id)
    if not slug:
        return {"ok": True, "messages": []}
    cfg = {"configurable": {"thread_id": session_id}}
    tup = await FileCheckpointer(slug).aget_tuple(cfg)
    if not tup or not tup.checkpoint:
        return {"ok": True, "messages": []}
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    attached = (tup.checkpoint.get("channel_values") or {}).get("attached_files") or []
    meta, _ = (_find_meta(session_id) or ({}, None))
    agent_id = meta.get("agent_id") if isinstance(meta, dict) else None
    last_error = (meta.get("last_error") or None) if isinstance(meta, dict) else None
    return {
        "ok": True,
        "messages": _messages_to_ui(messages, agent_id, attached),
        "last_error": last_error or None,
    }


@app.get("/api/skills")
async def list_skills(project_slug: str | None = None) -> list[dict]:
    skills = SkillLoader(project_slug=project_slug).load()
    return [
        {
            "name": s.name,
            "description": s.description,
            "trigger": s.trigger,
            "tools": s.allowed_tools,
            "builtin": s.builtin,
        }
        for s in skills
    ]


@app.get("/api/mcp")
async def list_mcp() -> dict:
    if not _mcp:
        return {"servers": [], "tools": []}
    return {
        "servers": list(_mcp.ensure_loaded().keys()),
        "tools": _mcp.list_tools(),
    }


@app.get("/api/settings")
async def get_settings() -> dict:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


# ---- providers (Settings → 模型 API) ----
@app.get("/api/providers")
async def get_providers() -> dict:
    settings = (
        json.loads(paths.settings_path().read_text() or "{}")
        if paths.settings_path().exists()
        else {}
    )
    return {
        "default_provider": prov_mod.get_default_provider(settings),
        "providers": prov_mod.load_providers(settings),
    }


class PutProvidersRequest(BaseModel):
    providers: dict
    default_provider: str | None = None


def _refresh_session_metas() -> None:
    """Re-resolve every session meta's provider/model against current config.

    Session metas persist provider/model from creation time; the topbar label
    and rebuilt graphs (``_ensure_session``) read them, so after a provider
    config change — or on startup, to heal metas frozen by older builds — they
    must be re-resolved. Precedence mirrors ``_resolve_provider_model`` minus
    explicit request overrides: an enabled agent provider, else the enabled
    global default.
    """
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    projects_root = paths.home() / "projects"
    if not projects_root.is_dir():
        return
    for slug_dir in sorted(projects_root.iterdir()):
        if not slug_dir.is_dir():
            continue
        slug = slug_dir.name
        metas = _session_meta_list(slug)
        if not metas:
            continue
        changed = False
        for m in metas:
            ag = _agent_lookup(m.get("agent_id"))
            provider = next(
                (
                    c
                    for c in [
                        ag.provider if ag else None,
                        prov_mod.get_default_provider(),
                    ]
                    if _enabled(c)
                ),
                None,
            ) or prov_mod.get_default_provider()
            model = (ag.model if ag and ag.model else None) or prov_mod.model_for_provider(
                providers, provider
            )
            if m.get("provider") != provider or m.get("model") != model:
                m["provider"] = provider
                m["model"] = model
                changed = True
        if changed:
            paths.session_index_path(slug).write_text(
                json.dumps(metas, indent=2, ensure_ascii=False)
            )


@app.put("/api/providers")
async def put_providers(req: PutProvidersRequest) -> dict:
    saved = prov_mod.save_providers(req.providers)
    default = req.default_provider
    if default:
        settings = (
            json.loads(paths.settings_path().read_text() or "{}")
            if paths.settings_path().exists()
            else {}
        )
        settings["default_provider"] = default
        paths.settings_path().write_text(
            json.dumps(settings, indent=2, ensure_ascii=False)
        )
    else:
        default = prov_mod.get_default_provider()
    # Evict all cached session graphs so the next WS connection rebuilds
    # with the freshly saved model/provider — otherwise existing sessions
    # keep using the LLM client that was frozen at session creation.
    _SESSIONS.clear()
    # Session metas persist provider/model from creation time; the topbar and
    # rebuilt graphs read them, so re-resolve them against the just-saved
    # config.
    _refresh_session_metas()
    return {"ok": True, "providers": saved, "default_provider": default}


@app.post("/api/providers/{provider_id}/verify")
async def verify_provider(provider_id: str) -> dict:
    return prov_mod.verify(provider_id)


# ---- agents ----
@app.post("/api/providers/{provider_id}/search_probe")
def provider_search_probe(provider_id: str) -> dict:
    """User-triggered (the 测试联网 button) probe of the model's built-in web
    search. Sync so the network round-trip runs in the threadpool, not on the
    event loop."""
    from . import providers as _prov

    return _prov.search_probe(provider_id)


@app.get("/api/agents")
async def list_agents_endpoint() -> list[dict]:
    return [a.to_dict() for a in agents_reg.list_agents()]


@app.post("/api/agents")
async def create_agent_endpoint(data: dict) -> dict:
    try:
        agent = agents_reg.create_agent(data).to_dict()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@app.put("/api/agents/{agent_id}")
async def update_agent_endpoint(agent_id: str, data: dict) -> dict:
    try:
        agent = agents_reg.update_agent(agent_id, data).to_dict()
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("agents.changed", {})
    return {"ok": True, "agent": agent}


@app.delete("/api/agents/{agent_id}")
async def delete_agent_endpoint(agent_id: str) -> dict:
    ok = agents_reg.delete_agent(agent_id)
    if ok:
        await _push_global_event("agents.changed", {})
    return {"ok": ok}


# ---- todos (global daily) ----
@app.get("/api/todos")
async def list_todos_endpoint() -> list[dict]:
    return todo_store.list_todos()


@app.post("/api/todos")
async def create_todo_endpoint(data: dict) -> dict:
    try:
        todo = todo_store.create_todo(data)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("todos.changed", {})
    return {"ok": True, "todo": todo}


@app.patch("/api/todos/{todo_id}")
async def update_todo_endpoint(todo_id: str, data: dict) -> dict:
    before = next((t for t in todo_store.list_todos() if t["id"] == todo_id), None)
    updated = todo_store.update_todo(todo_id, data)
    if updated is None:
        return {"ok": False, "error": "not found"}
    # Local done → platform: for every ext ref whose provider has auto_push,
    # trigger a todo-push workflow run (fully automatic; the checkbox IS the
    # confirmation). Failures never roll back the local state — the ledger
    # records them and the panel offers retry.
    if data.get("done") and before is not None and not before.get("done"):
        last_run = None
        for item in updated.get("ext") or []:
            pid = item.get("provider") or ""
            if not item.get("id"):
                continue
            prov = todo_providers.get_todo_provider(pid)
            if not prov or not prov.get("auto_push", True):
                continue
            ready, why = _provider_ready(prov)
            if not ready:
                _log.warning("todo_push_skipped todo=%s provider=%s reason=%s", todo_id, pid, why)
                continue
            run = _trigger_todo_workflow(
                "todo-push",
                prov,
                {
                    "ext_id": str(item["id"]),
                    "title": updated["title"],
                    "url": str(item.get("url") or ""),
                },
            )
            if run:
                sync_ledger.append(todo_id, pid, str(item["id"]), "push", run["id"])
                last_run = run
        if last_run:
            updated = (
                todo_store.update_todo(todo_id, {"links": {"workflow_id": last_run["id"]}})
                or updated
            )
    await _push_global_event("todos.changed", {})
    return {"ok": True, "todo": updated}


def _trigger_todo_workflow(wf_id: str, prov: dict, ctx: dict) -> dict | None:
    """Start a todo-pull/todo-push run with the provider's skill/mcp resolved
    into context (the generic agent node injects the skill and unlocks the
    provider's MCP server tools via {{mcp}})."""
    import asyncio

    wf = wf_store.get_def(wf_id)
    if not wf:
        return None
    skill = todo_providers.resolve_skill_for(prov["id"], prov)
    run = wf_store.create_run(wf)
    override = {
        **ctx,
        "provider": prov["id"],
        "skill": skill or prov["id"],
        "mcp": str(prov.get("mcp") or ""),
    }
    task = asyncio.create_task(_run_workflow_bg(run["id"], wf_id, override, None))
    _WF_RUN_TASKS[run["id"]] = task
    return run


def _provider_ready(prov: dict) -> tuple[bool, str]:
    """A provider can sync iff it has an injectable skill OR its MCP server is
    connected with tools. Gives actionable errors instead of pointless runs."""
    if todo_providers.resolve_skill_for(prov["id"], prov):
        return True, ""
    srv = prov.get("mcp")
    if srv:
        if _mcp and _mcp.server_tools(srv):
            return True, ""
        return False, f"MCP 服务未连接或无工具: {srv}"
    return False, f"provider {prov['id']} 既无 skill 也无可用 MCP（settings → todo_providers）"


@app.get("/api/todo-providers")
async def list_todo_providers_endpoint() -> dict:
    """Discovered external TODO platforms (skill declarations + settings)."""
    return {"ok": True, "providers": todo_providers.list_todo_providers()}


@app.get("/api/todos/sync-status")
async def todo_sync_status_endpoint() -> dict:
    """Recent todo<->platform sync events (panel badges / retry affordance)."""
    return {"ok": True, "entries": sync_ledger.latest(100)}


@app.post("/api/todos/pull")
async def todo_pull_endpoint(data: dict) -> dict:
    """Pull direction: mirror a provider's open todos into the local list."""
    pid = (data or {}).get("provider") or ""
    prov = todo_providers.get_todo_provider(pid)
    if not prov:
        return {"ok": False, "error": f"unknown todo provider: {pid}"}
    ready, why = _provider_ready(prov)
    if not ready:
        return {"ok": False, "error": why}
    run = _trigger_todo_workflow("todo-pull", prov, {})
    if not run:
        return {"ok": False, "error": "todo-pull workflow missing"}
    sync_ledger.append("", pid, "", "pull", run["id"])
    return {"ok": True, "run": run}


@app.post("/api/todos/{todo_id}/push")
async def todo_push_endpoint(todo_id: str, data: dict) -> dict:
    """Manual push / retry: re-trigger todo-push for one ext ref."""
    todo = next((t for t in todo_store.list_todos() if t["id"] == todo_id), None)
    if todo is None:
        return {"ok": False, "error": "not found"}
    pid = (data or {}).get("provider") or ""
    item = next((x for x in (todo.get("ext") or []) if x.get("provider") == pid), None)
    prov = todo_providers.get_todo_provider(pid)
    if not item or not prov:
        return {"ok": False, "error": f"no ext ref for provider: {pid}"}
    ready, why = _provider_ready(prov)
    if not ready:
        return {"ok": False, "error": why}
    run = _trigger_todo_workflow(
        "todo-push",
        prov,
        {"ext_id": str(item["id"]), "title": todo["title"], "url": str(item.get("url") or "")},
    )
    if not run:
        return {"ok": False, "error": "todo-push workflow missing"}
    sync_ledger.append(todo_id, pid, str(item["id"]), "push", run["id"])
    return {"ok": True, "run": run}


@app.delete("/api/todos/{todo_id}")
async def delete_todo_endpoint(todo_id: str) -> dict:
    ok = todo_store.delete_todo(todo_id)
    if ok:
        await _push_global_event("todos.changed", {})
    return {"ok": ok}


# ---- settings (general) ----
@app.put("/api/settings")
async def put_settings(data: dict) -> dict:
    paths.settings_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


# ---- workflows ----
@app.get("/api/workflows")
async def list_workflows_endpoint() -> list[dict]:
    return wf_store.list_defs()


@app.post("/api/workflows")
async def create_workflow_endpoint(data: dict) -> dict:
    wf = wf_store.create_def(data)
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "workflow": wf}


@app.put("/api/workflows/{wf_id}")
async def update_workflow_endpoint(wf_id: str, data: dict) -> dict:
    wf = wf_store.update_def(wf_id, data)
    if wf:
        await _push_global_event("workflows.changed", {})
    return {"ok": bool(wf), "workflow": wf}


@app.delete("/api/workflows/{wf_id}")
async def delete_workflow_endpoint(wf_id: str) -> dict:
    if wf_storemod.is_system_def(wf_id):
        return {"ok": False, "error": "内置 workflow，不可删除"}
    ok = wf_store.delete_def(wf_id)
    if ok:
        await _push_global_event("workflows.changed", {})
    return {"ok": ok}


@app.get("/api/workflow_runs")
async def list_workflow_runs_endpoint() -> list[dict]:
    return wf_store.list_runs()


@app.get("/api/workflows/{wf_id}")
async def get_workflow_endpoint(wf_id: str) -> dict:
    """Full definition view: current DSL + version + legacy steps projection."""
    wf = wf_store.get_def(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"ok": True, "workflow": wf}


@app.get("/api/workflows/{wf_id}/versions")
async def list_workflow_versions_endpoint(wf_id: str) -> dict:
    return {"ok": True, "versions": wf_store.list_versions(wf_id)}


@app.get("/api/workflows/{wf_id}/versions/diff")
async def diff_workflow_versions_endpoint(wf_id: str, a: int, b: int) -> dict:
    diff = wf_store.diff_versions(wf_id, a, b)
    if diff is None:
        raise HTTPException(status_code=404, detail="version not found")
    return {"ok": True, "a": a, "b": b, "diff": diff}


@app.get("/api/workflows/{wf_id}/versions/{n}")
async def get_workflow_version_endpoint(wf_id: str, n: int) -> dict:
    v = wf_store.get_version(wf_id, n)
    if v is None:
        raise HTTPException(status_code=404, detail="version not found")
    return {"ok": True, "version": n, "dsl": v}


@app.post("/api/workflows/{wf_id}/rollback")
async def rollback_workflow_endpoint(wf_id: str, data: dict) -> dict:
    to = (data or {}).get("to")
    if not isinstance(to, int):
        raise HTTPException(status_code=400, detail="integer 'to' required")
    wf = wf_store.rollback(wf_id, to, commit=(data or {}).get("commit", ""))
    if not wf:
        raise HTTPException(status_code=404, detail="workflow/version not found")
    return {"ok": True, "workflow": wf}


# ---- P6: synthesize a workflow DSL draft from a session's conversation ----
_SYNTHESIZE_PROMPT = (
    "You are a workflow synthesizer. Given a conversation trace between a user and "
    "an agent (including tool calls), produce a reusable workflow DSL that captures "
    "the repeatable process as a directed graph.\n\n"
    "DSL object shape:\n"
    '{\n  "name": "<short imperative name>",\n  "description": "<one line>",\n'
    '  "entry": "<first node id>",\n'
    '  "context": {"schema": {"type":"object","properties":{...}}, "initial": {...}},\n'
    '  "nodes": [ {"id","type","agent","goal", ...} ],\n'
    '  "edges": [ {"from","to"} ]\n}\n\n'
    "Node types:\n"
    '- step: {"id","type":"step","agent":"dev|research|writer","goal":"<instruction>"}\n'
    '- branch: {"id","type":"branch","cases":[{"when":"<expr>","then":"<id>"}],"default":"<id>"}\n'
    '- loop: {"id","type":"loop","over":"<expr e.g. context.items>","as":"<var>","body":"<body id>","max_iters":<int>}\n\n'
    "Rules:\n"
    "- `entry` MUST be an existing node id; every edge endpoint MUST exist.\n"
    "- A loop's body returns to the loop head automatically: do NOT add an edge FROM the body; reference the loop item via {{<as>}}.\n"
    "- A branch routes via cases/default: do NOT add plain edges from a branch.\n"
    "- Put any per-run inputs the conversation revealed into context.schema + context.initial.\n"
    "- Default to a simple linear step chain; only add branch/loop when the trace clearly shows conditionals or repetition.\n"
    "- Agents: dev (code/actions), research (read/summarise), writer (draft text).\n\n"
    "Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _trace_text(messages) -> str:
    """Compact readable trace of a session for the synthesizer."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    lines: list[str] = []
    for m in messages or []:
        if isinstance(m, HumanMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"USER: {c[:500]}")
        elif isinstance(m, AIMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            if c.strip():
                lines.append(f"AGENT: {c[:500]}")
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"  -> tool {tc.get('name')}({json.dumps(tc.get('args') or {}, ensure_ascii=False)[:200]})")
        elif isinstance(m, ToolMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"  <= {getattr(m, 'name', 'tool')}: {c[:200]}")
    return "\n".join(lines)


def _extract_json_obj(text: str) -> dict | None:
    import re

    text = (text or "").strip()
    # strip markdown fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    # fallback: first balanced {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            v = json.loads(text[start : end + 1])
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None
    return None


@app.post("/api/workflows/summarize-from-session")
async def summarize_session_to_dsl(data: dict) -> dict:
    """Distill a session's conversation into a workflow DSL *draft* (not saved).
    The UI then creates a workflow from it (version 1) or opens the dev agent."""
    from langchain_core.messages import HumanMessage, SystemMessage

    session_id = (data or {}).get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    slug = _session_slug(session_id)
    if not slug:
        raise HTTPException(status_code=404, detail="session not found")
    tup = await FileCheckpointer(slug).aget_tuple({"configurable": {"thread_id": session_id}})
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") if tup and tup.checkpoint else []
    if not messages:
        raise HTTPException(status_code=400, detail="session has no messages")
    trace = _trace_text(messages)
    provider = (data or {}).get("provider") or prov_mod.get_default_provider()
    try:
        model = build_model(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"model unavailable: {e}")
    resp = await model.ainvoke(
        [SystemMessage(content=_SYNTHESIZE_PROMPT), HumanMessage(content=trace)]
    )
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    dsl = _extract_json_obj(raw)
    if not isinstance(dsl, dict):
        return {"ok": False, "error": "model did not return a JSON DSL object", "raw": raw[:1000]}
    dsl = wf_dsl.normalize_dsl(dsl)
    errs = wf_dsl.validate_dsl(dsl)
    if errs:
        return {"ok": False, "error": "synthesized DSL invalid: " + "; ".join(errs), "dsl": dsl}
    return {"ok": True, "dsl": dsl, "source_session_id": session_id}


# ---- workflow execution (P2 engine) ----
def _wf_mcp_tools() -> list:
    try:
        return _mcp.all_langchain_tools() if _mcp else []
    except Exception:
        return []


def _wf_build_deps(run_id: str, workflow_id: str):
    """Resolve (wf, dsl, model, tools) for a run by forking its source agent."""
    wf = wf_store.get_def(workflow_id)
    if not wf:
        return None, None, None, None
    dsl = wf["dsl"]
    src_agent_id = None
    for n in dsl.get("nodes") or []:
        if n.get("agent"):
            src_agent_id = n["agent"]
            break
    src_agent_id = src_agent_id or wf.get("agent_id") or "dev"
    fork = agents_reg.fork_agent(src_agent_id, f"wf-{run_id[:8]}-{src_agent_id}")
    model = build_model(fork.provider, fork.model or None)
    tools = build_all_tools(_wf_mcp_tools())
    return wf, dsl, model, tools, fork.id


def _set_run_status(run_id: str, status: str) -> None:
    run = wf_store.get_run(run_id)
    if run:
        run["status"] = status
        run["updated"] = time.time()
        wf_storemod._write_json(wf_storemod._run_path(run_id), run)


async def _drive_run_events(run_id: str, present_in: str | None, wf: dict, agen) -> None:
    """Persist + push each engine event; keep run step status + terminal state in sync."""
    node_to_step = {s["id"]: s["id"] for s in wf.get("steps", [])}
    async for ev in agen:
        wf_events.append_event(run_id, ev.get("kind", ""), **{
            k: v for k, v in ev.items() if k not in ("kind", "run_id")
        })
        await _push_session_event(present_in, "run.event", {"run_id": run_id, "payload": ev})
        kind = ev.get("kind")
        nid = ev.get("node_id")
        if kind == "node_enter" and nid in node_to_step:
            wf_store.update_step(run_id, node_to_step[nid], "running")
        elif kind == "node_exit" and nid in node_to_step:
            wf_store.update_step(run_id, node_to_step[nid], "done" if ev.get("status") != "failed" else "failed")
        elif kind == "done":
            _set_run_status(run_id, "done")
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "done"})
        elif kind == "paused":
            _set_run_status(run_id, "paused")
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "paused"})
        elif kind == "error":
            _set_run_status(run_id, "failed")
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "failed"})


async def _run_workflow_bg(run_id: str, workflow_id: str, context_override: dict | None, present_in: str | None = None) -> None:
    """Background driver: fork agent, stream the engine, persist + push events."""
    from .workflows import engine as wf_engine

    wf, dsl, model, tools, fork_id = _wf_build_deps(run_id, workflow_id)
    if not wf:
        return
    try:
        await _push_session_event(present_in, "run.bind", {"run_id": run_id, "workflow_id": workflow_id, "present_in_session_id": present_in})
        agen = wf_engine.run_workflow(dsl, run_id=run_id, model=model, tools=tools, context_override=context_override)
        await _drive_run_events(run_id, present_in, wf, agen)
        sync_ledger.set_status(run_id, "ok")
    except Exception as exc:  # pragma: no cover - defensive
        wf_events.append_event(run_id, "error", error=f"{type(exc).__name__}: {exc}")
        _set_run_status(run_id, "failed")
        sync_ledger.set_status(run_id, "failed", f"{type(exc).__name__}: {exc}")
        await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "failed"})
    finally:
        # The fork is a per-run scratch agent; drop it so the Agents list
        # doesn't accumulate wf-* clutter (reruns/resumes re-fork idempotently).
        try:
            agents_reg.delete_agent(fork_id)
        except Exception:
            pass
        # Headless runs (todo sync et al.) have no present_in session; push
        # globally so the Workflow panel lists them without a manual refresh.
        await _push_global_event("workflows.changed", {})


async def _resume_workflow_bg(run_id: str, workflow_id: str, resume_value: dict, present_in: str | None = None) -> None:
    """Background driver to continue a paused run (human/supervisor decision)."""
    from .workflows import engine as wf_engine

    wf, dsl, model, tools, fork_id = _wf_build_deps(run_id, workflow_id)
    if not wf:
        return
    try:
        _set_run_status(run_id, "running")
        agen = wf_engine.resume_workflow(dsl, run_id=run_id, model=model, tools=tools, resume_value=resume_value)
        await _drive_run_events(run_id, present_in, wf, agen)
    except Exception as exc:  # pragma: no cover
        wf_events.append_event(run_id, "error", error=f"{type(exc).__name__}: {exc}")
        _set_run_status(run_id, "failed")
        await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "failed"})
    finally:
        # paused-at-interrupt runs re-fork idempotently on next resume
        try:
            agents_reg.delete_agent(fork_id)
        except Exception:
            pass


@app.post("/api/workflow_runs")
async def create_workflow_run_endpoint(data: dict) -> dict:
    """Trigger a workflow run: creates the run (bound to a session for in-chat
    rendering), forks the agent, executes in the background. Returns immediately."""
    import asyncio

    data = data or {}
    workflow_id = data.get("workflow_id")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id required")
    wf = wf_store.get_def(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    session_id = data.get("session_id")
    present_in = data.get("present_in_session_id") or session_id
    run = wf_store.create_run(wf, session_id=session_id, present_in_session_id=present_in)
    task = asyncio.create_task(_run_workflow_bg(run["id"], workflow_id, data.get("context_override"), present_in))
    _WF_RUN_TASKS[run["id"]] = task
    return {"ok": True, "run": run}


@app.post("/api/workflow_runs/{run_id}/cancel")
async def cancel_workflow_run_endpoint(run_id: str) -> dict:
    """Cancel a running workflow run (stops the background task)."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    task = _WF_RUN_TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()
    _set_run_status(run_id, "cancelled")
    await _push_session_event(run.get("present_in_session_id"), "run.status", {"run_id": run_id, "status": "cancelled"})
    return {"ok": True, "status": "cancelled"}


@app.post("/api/workflow_runs/{run_id}/resume")
async def resume_workflow_run_endpoint(run_id: str, data: dict) -> dict:
    """Resume a paused run with a value (e.g. {"decision":..., "context_patch":{...}})."""
    import asyncio

    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "paused":
        raise HTTPException(status_code=409, detail=f"run not paused (status={run.get('status')})")
    task = asyncio.create_task(_resume_workflow_bg(run_id, run["workflow_id"], data or {}, run.get("present_in_session_id")))
    _WF_RUN_TASKS[run_id] = task
    return {"ok": True, "status": "resuming"}


@app.post("/api/workflow_runs/{run_id}/decide")
async def decide_workflow_run_endpoint(run_id: str, data: dict) -> dict:
    """Supervisor/human decision = resume with {"decision","context_patch"}."""
    data = data or {}
    value = {"decision": data.get("decision"), "context_patch": data.get("context_patch") or {}}
    return await resume_workflow_run_endpoint(run_id, value)


@app.get("/api/workflow_runs/{run_id}")
async def get_workflow_run_endpoint(run_id: str) -> dict:
    return {"ok": True, "run": wf_store.get_run(run_id)}


@app.get("/api/workflow_runs/{run_id}/events")
async def workflow_run_events_endpoint(
    run_id: str, node_id: str | None = None, kind: str | None = None
) -> dict:
    return {"ok": True, "events": wf_events.read_events(run_id, node_id=node_id, kind=kind)}


@app.post("/api/workflow_runs/{run_id}/_await")
async def await_workflow_run_endpoint(run_id: str) -> dict:
    """Test/ops helper: await the background run task so callers can observe the
    terminal state deterministically. Not required by the UI (which polls events)."""
    task = _WF_RUN_TASKS.get(run_id)
    err: str | None = None
    if task is not None:
        try:
            await task
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "run": wf_store.get_run(run_id), "error": err}


# ---- artifacts ----
@app.get("/api/artifacts")
async def list_artifacts_endpoint(
    project_slug: str = "default", session_id: str | None = None
) -> list[dict]:
    """Artifacts belong to a session: pass ``session_id`` to scope the list to
    that session (the Artifacts panel does this). Omit for all (back-compat)."""
    return art_store.list_artifacts(project_slug, session_id)


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_endpoint(artifact_id: str, project_slug: str = "default") -> dict:
    """Remove an artifact entry from the panel. The underlying file (if any)
    is NOT touched on disk — deletion is reference-only and recoverable."""
    return {"ok": art_store.delete_artifact(project_slug, artifact_id)}


def _heal_artifact_ref(art: dict, project_slug: str) -> str | None:
    """Re-point an artifact whose file was moved — typical case: generated
    markdown later moved/copied into the knowledge vault — so artifact links
    and previews keep working from the new home instead of showing "file
    missing". Searches the vault by basename/stem; on hit, rewrites the
    artifact ref and relocates (or registers) the file-registry entry.
    Returns the new path, or None when nothing was found."""
    ref = art.get("ref") or ""
    if not ref or Path(ref).is_file():
        return None
    try:
        from .knowledge.config import load_knowledge_config

        vault = Path(str(load_knowledge_config().vault_path)).expanduser()
        if not vault.is_dir():
            return None
        name, stem = Path(ref).name, Path(ref).stem
        hit = next(
            (c for c in sorted(vault.rglob("*.md")) if c.name == name or c.stem == stem),
            None,
        )
        if hit is None:
            return None
        new = str(hit)
        art_store.set_ref(project_slug, art["id"], new)
        reg = files_mod.get_registry(project_slug)
        old_entry = reg.find_by_path(ref)
        if old_entry:
            reg.relocate(old_entry["id"], new)
        else:
            reg.register(
                hit.name, new, session_id=art.get("session_id"), artifact_id=art["id"]
            )
        return new
    except Exception:
        _log.exception("artifact_ref_heal_failed artifact=%s", art.get("id"))
        return None


@app.get("/api/artifacts/{artifact_id}/metadata")
async def artifact_metadata_endpoint(artifact_id: str, project_slug: str = "default") -> dict:
    """Full inspector payload for one artifact: the panel record, its file
    registry entry (size/mtime/kind), whether the file still exists on disk,
    and the EXACT schema summary that prompt injection would use — with its
    provenance (user override vs auto-computed) so the UI can show both."""
    from .files import extractors as files_ex

    art = art_store.get_artifact(project_slug, artifact_id)
    if art is None:
        return {"ok": False, "error": "not found"}
    file_entry = None
    exists = False
    schema = ""
    schema_source = ""
    ref = art.get("ref") or ""
    if art.get("kind") == "file" and ref:
        reg = files_mod.get_registry(project_slug)
        if not Path(ref).is_file():
            # The file may have been moved into the knowledge vault —
            # re-point ref + registry so the link heals instead of breaking.
            if _heal_artifact_ref(art, project_slug):
                ref = art.get("ref") or ref
        file_entry = reg.find_by_path(ref)
        path = (file_entry or {}).get("path") or ref
        exists = Path(path).is_file()
        override = (art.get("schema") or "").strip()
        effective_kind = (file_entry or {}).get("kind") or files_ex.classify(path)
        if override:
            schema, schema_source = override, "override"
        elif effective_kind in ("spreadsheet", "table"):
            schema = _compact_schema(path)
            schema_source = "computed" if schema else ""
    return {
        "ok": True,
        "artifact": art,
        "file": file_entry,
        "exists": exists,
        "schema": schema,
        "schema_source": schema_source,
    }


@app.put("/api/artifacts/{artifact_id}")
async def update_artifact_endpoint(
    artifact_id: str, data: dict, project_slug: str = "default"
) -> dict:
    """User corrections from the metadata inspector. ``name/kind/ref/schema``
    land on the artifact record (``schema`` becomes the injection override);
    ``file_kind`` corrects the registry's classification, which steers the
    prompt's tool guidance (analyze_table vs parse_document)."""
    if art_store.get_artifact(project_slug, artifact_id) is None:
        return {"ok": False, "error": "not found"}
    patch = {k: data[k] for k in ("name", "kind", "ref", "schema") if data.get(k) is not None}
    updated = art_store.update_artifact(project_slug, artifact_id, patch)
    if updated is None:
        return {"ok": False, "error": "名称不能为空"}
    file_kind = (data.get("file_kind") or "").strip()
    if file_kind and updated.get("kind") == "file" and updated.get("ref"):
        files_mod.get_registry(project_slug).set_kind(updated["ref"], file_kind)
    return {"ok": True, "artifact": updated}


# ---- files (upload / preview — see docs/file-parsing-research.md §7) ----
UPLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_UNSAFE_NAME_RE = re.compile(r"[^\w一-鿿.\-]+", re.UNICODE)


def _safe_upload_name(name: str) -> str:
    base = Path(name).name
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")
    return cleaned or "file"


@app.post("/api/files")
async def upload_file_endpoint(
    session_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> dict:
    """Upload a file attached in the composer; lands in the session's files dir
    under ``uploads/`` and becomes a ``kind=file`` artifact (session-scoped)."""
    meta = _resolve_session_meta(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    slug = meta.get("project_slug") or "default"
    name = _safe_upload_name(file.filename or "file")
    data = await file.read()
    if len(data) > UPLOAD_MAX_BYTES:
        return {
            "ok": False,
            "error": f"文件过大（上限 {UPLOAD_MAX_BYTES // 1024 // 1024}MB）",
        }
    dest_dir = paths.session_uploads_dir(slug, session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    dest.write_bytes(data)
    # ref must match the registry's normalized path (symlink-safe), or the
    # UI's artifact → preview/download lookup by path misses (macOS /tmp).
    art = art_store.add_artifact(slug, "file", name, files_mod.norm_path(dest), session_id)
    entry = files_mod.get_registry(slug).register(
        name,
        dest,
        mime=file.content_type or "",
        size=len(data),
        session_id=session_id,
        artifact_id=art.get("id"),
    )
    return {"ok": True, "file": entry}


@app.get("/api/files")
async def list_files_endpoint(
    project_slug: str = "default", session_id: str | None = None
) -> list[dict]:
    reg = files_mod.get_registry(project_slug)
    return reg.list_session(session_id) if session_id else reg.list_all()


@app.post("/api/files/attach-path")
async def attach_file_by_path_endpoint(req: dict) -> dict:
    """Attach an OS file the user dragged into the desktop app.

    WKWebView can't expose dropped files to JS, so the Tauri shell forwards the
    native path here; the sidecar (same filesystem) copies it into the session's
    files dir and registers it like an upload.
    """
    import shutil

    from .files import extractors as files_ex

    session_id = req.get("session_id") or ""
    src = req.get("path") or ""
    meta = _resolve_session_meta(session_id)
    if meta is None:
        return {"ok": False, "error": f"session not found: {session_id}"}
    slug = meta.get("project_slug") or "default"
    p = Path(src).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在: {src}"}
    name = p.name
    dest_dir = paths.session_uploads_dir(slug, session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    try:
        shutil.copyfile(p, dest)
    except OSError as e:
        return {"ok": False, "error": f"无法读取文件: {e}"}
    kind = files_ex.classify(dest)
    art = art_store.add_artifact(slug, "file", name, files_mod.norm_path(dest), session_id)
    entry = files_mod.get_registry(slug).register(
        name,
        dest,
        kind=kind,
        size=dest.stat().st_size,
        session_id=session_id,
        artifact_id=art.get("id"),
    )
    return {"ok": True, "file": entry}


@app.post("/api/debug-log")
async def debug_log_endpoint(data: dict) -> dict:
    """Temporary: frontend drop/upload telemetry for diagnosing WKWebView DnD."""
    # print (not _log) — stdout is forwarded to sidecar.log by the Tauri shell;
    # the Python logger isn't wired to a visible sink in the frozen build.
    print("DEBUG-DROP " + json.dumps(data, ensure_ascii=False, default=str), flush=True)
    return {"ok": True}


@app.get("/api/files/{file_id}/preview")
async def file_preview_endpoint(
    file_id: str,
    sheet: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Paginated grid for spreadsheets/tables; extracted markdown for docs."""
    from .files import extractors as files_ex
    from .files import preview as files_preview

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        return {"ok": False, "error": f"file not found: {file_id}"}
    if not Path(entry["path"]).is_file() and entry.get("artifact_id"):
        # File may have been moved into the knowledge vault — heal the
        # registry entry (via its artifact) and retry from the new path.
        slug = entry.get("project_slug") or "default"
        art = art_store.get_artifact(slug, entry["artifact_id"])
        if art is not None and _heal_artifact_ref(art, slug):
            entry = files_mod.get_by_id(file_id) or entry
    try:
        payload = files_preview.build_preview(
            entry["path"], sheet=sheet, offset=offset, limit=limit
        )
    except (files_ex.UnsupportedFormat, files_ex.ExtractorUnavailable) as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError:
        return {"ok": False, "error": "文件已被移动或删除"}
    except Exception as e:  # parse failure → actionable error, not 500
        return {"ok": False, "error": f"预览失败: {type(e).__name__}: {e}"}
    # a fresh preview counts as "seen": clear the stale badge + sync mtime
    try:
        entry["mtime"] = Path(entry["path"]).stat().st_mtime
    except OSError:
        pass
    entry["stale"] = False
    return {
        "ok": True,
        "file": {
            "id": entry["id"],
            "name": entry["name"],
            "kind": entry.get("kind", ""),
            "path": entry["path"],
            "stale": entry.get("stale", False),
        },
        **payload,
    }


def _attachment_headers(filename: str) -> dict[str, str]:
    """Content-Disposition for downloads; RFC 5987 filename* for UTF-8 names."""
    from urllib.parse import quote

    fallback = filename.encode("ascii", "replace").decode().replace('"', "_")
    return {
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'
        )
    }


@app.get("/api/files/{file_id}/download")
async def file_download_endpoint(
    file_id: str,
    fmt: str = "raw",
    sheet: str | None = None,
) -> Any:
    """Download the original file (fmt=raw) or export one sheet as CSV
    (fmt=csv — spreadsheet/table kinds only; ``sheet`` selects which)."""
    from starlette.responses import Response

    from .files import extractors as files_ex
    from .files import preview as files_preview

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"file not found: {file_id}")
    p = Path(entry["path"])
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件已被移动或删除")
    if fmt == "raw":
        from starlette.responses import FileResponse

        return FileResponse(
            p,
            filename=entry.get("name") or p.name,
            headers=_attachment_headers(entry.get("name") or p.name),
        )
    if fmt == "csv":
        try:
            name, data = files_preview.build_csv_export(
                p, sheet=sheet, name=entry.get("name")
            )
        except files_ex.UnsupportedFormat as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except files_ex.ExtractorUnavailable as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        except Exception as e:  # parse failure → actionable error, not bare 500
            raise HTTPException(
                status_code=500, detail=f"导出失败: {type(e).__name__}: {e}"
            ) from e
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers=_attachment_headers(name),
        )
    raise HTTPException(status_code=400, detail=f"unsupported fmt: {fmt}")


@app.post("/api/files/{file_id}/save-to-downloads")
async def save_file_to_downloads_endpoint(
    file_id: str, req: dict | None = None
) -> dict:
    """Copy the file (or a CSV export of it) into the OS Downloads folder.

    WKWebView can't trigger browser downloads, so the desktop UI calls this
    instead: the sidecar (same filesystem, same user) writes the copy and
    reports the destination path. Body: ``{"fmt": "raw"|"csv", "sheet"?}``.
    """
    import os
    import shutil

    from .files import extractors as files_ex
    from .files import preview as files_preview

    req = req or {}
    fmt = req.get("fmt") or "raw"
    sheet = req.get("sheet")
    entry = files_mod.get_by_id(file_id)
    if entry is None:
        return {"ok": False, "error": f"file not found: {file_id}"}
    p = Path(entry["path"])
    if not p.is_file():
        return {"ok": False, "error": "文件已被移动或删除"}
    downloads = Path(os.environ.get("GINNO_DOWNLOADS") or (Path.home() / "Downloads"))
    try:
        downloads.mkdir(parents=True, exist_ok=True)
        if fmt == "raw":
            name = entry.get("name") or p.name
            dest = _unique_dest(downloads / name)
            shutil.copyfile(p, dest)
        elif fmt == "csv":
            name, data = files_preview.build_csv_export(
                p, sheet=sheet, name=entry.get("name")
            )
            dest = _unique_dest(downloads / name)
            dest.write_bytes(data)
        else:
            return {"ok": False, "error": f"unsupported fmt: {fmt}"}
    except files_ex.UnsupportedFormat as e:
        return {"ok": False, "error": str(e)}
    except files_ex.ExtractorUnavailable as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": f"写入 Downloads 失败: {e}"}
    return {"ok": True, "path": str(dest), "name": dest.name}


# Collision-free destination naming lives in files.registry (shared with the
# session-files relocation / migration paths).
_unique_dest = files_mod.unique_dest


# ---- session files management (Settings → 会话文件) ----
def _session_file_guard(slug: str, session_id: str, sub: str | None) -> Path | None:
    """Resolve ``sub`` (a path relative to the session dir) and require it to
    stay inside ``sessions/<session_id>/``. Returns the resolved Path, or None
    if the session_id/sub is malformed or escapes the dir (path traversal)."""
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return None
    base = paths.session_files_dir(slug, session_id).resolve()
    if not sub:
        return base
    target = (base / sub).resolve()
    try:
        if not target.is_relative_to(base):
            return None
    except ValueError:
        return None
    return target


def _is_orphaned_session(slug: str, session_id: str) -> bool:
    """True when the session no longer exists in the project's session index.

    Deletion of session files is restricted to orphaned sessions: an active
    session's files are "live" (in use by the conversation), so they can be
    browsed/revealed but not removed from Settings. Only once the session itself
    is deleted do its preserved files become cleanable.
    """
    if not session_id:
        return False
    return not any(m.get("id") == session_id for m in _session_meta_list(slug))


def _dir_stats(d: Path) -> tuple[int, int, float]:
    """(file_count, total_bytes, newest mtime) for a directory tree."""
    n = 0
    size = 0
    mtime = 0.0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                n += 1
                try:
                    st = f.stat()
                    size += st.st_size
                    mtime = max(mtime, st.st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return n, size, mtime


@app.get("/api/session-files/dirs")
async def list_session_file_dirs_endpoint() -> dict:
    """List every per-session files directory across all projects, including
    orphaned ones (session deleted but its files preserved)."""
    out: list[dict] = []
    projects_root = paths.home() / "projects"
    if projects_root.is_dir():
        for proj in sorted(projects_root.iterdir()):
            if not proj.is_dir():
                continue
            slug = proj.name
            sessions_root = paths.project_sessions_dir(slug)
            if not sessions_root.is_dir():
                continue
            metas = {m.get("id"): m for m in _session_meta_list(slug)}
            for d in sorted(sessions_root.iterdir()):
                if not d.is_dir():  # skips _index.json and <sid>.json checkpoints
                    continue
                sid = d.name
                meta = metas.get(sid)
                n, size, mtime = _dir_stats(d)
                out.append(
                    {
                        "project_slug": slug,
                        "session_id": sid,
                        "title": (meta or {}).get("title"),
                        "orphaned": meta is None,
                        "dir": str(d),
                        "file_count": n,
                        "total_bytes": size,
                        "mtime": mtime,
                    }
                )
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"ok": True, "sessions": out}


@app.get("/api/session-files/list")
async def list_session_files_endpoint(
    project_slug: str = "default", session_id: str = "", sub: str | None = None
) -> dict:
    target = _session_file_guard(project_slug, session_id, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_dir():
        return {"ok": True, "path": str(target), "entries": []}
    entries = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": st.st_size if child.is_file() else 0,
                "mtime": st.st_mtime,
            }
        )
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    base = paths.session_files_dir(project_slug, session_id).resolve()
    rel = str(target.relative_to(base)) if target != base else ""
    return {"ok": True, "path": rel, "entries": entries}


@app.post("/api/session-files/reveal")
async def reveal_session_file_endpoint(req: dict) -> dict:
    """Reveal a session file/dir in the OS file manager (Finder on macOS)."""
    import subprocess
    import sys

    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    target = _session_file_guard(slug, sid, sub)
    if target is None or not target.exists():
        return {"ok": False, "error": "文件不存在"}
    try:
        if sys.platform == "darwin":
            if target.is_dir():
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["open", "-R", str(target)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except OSError as e:
        return {"ok": False, "error": f"无法打开文件管理器: {e}"}
    return {"ok": True}


@app.delete("/api/session-files/file")
async def delete_session_file_endpoint(req: dict) -> dict:
    """Delete one file inside a session dir; also drops its registry entry and
    artifact panel row so the UI reflects the removal.

    Only files of an ORPHANED session (already deleted) can be removed — an
    active session's files are live and protected.
    """
    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    if not _is_orphaned_session(slug, sid):
        return {"ok": False, "error": "仅支持删除已删除会话的文件；请先删除该会话"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_file():
        return {"ok": False, "error": "文件不存在"}
    reg = files_mod.get_registry(slug)
    entry = reg.find_by_path(target)
    unregistered = False
    if entry is not None:
        art_id = entry.get("artifact_id")
        reg.unregister(entry["id"])
        if art_id:
            art_store.delete_artifact(slug, art_id)
        unregistered = True
    try:
        target.unlink()
    except OSError as e:
        return {"ok": False, "error": f"删除失败: {e}"}
    return {"ok": True, "unregistered": unregistered}


@app.delete("/api/session-files/dir")
async def delete_session_dir_endpoint(req: dict) -> dict:
    """Delete a subdirectory (or the whole session dir when ``path`` is omitted).
    Purges registry + artifact rows for the files inside first.

    Only an ORPHANED session's directory can be removed — an active session's
    files are live and protected.
    """
    import shutil

    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    if not _is_orphaned_session(slug, sid):
        return {"ok": False, "error": "仅支持删除已删除会话的文件；请先删除该会话"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_dir():
        return {"ok": False, "error": "目录不存在"}
    reg = files_mod.get_registry(slug)
    removed = 0
    prefix = str(target.resolve())
    for e in reg.list_all():
        p = e.get("path") or ""
        if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix + "\\"):
            art_id = e.get("artifact_id")
            reg.unregister(e["id"])
            if art_id:
                art_store.delete_artifact(slug, art_id)
            removed += 1
    try:
        shutil.rmtree(target)
    except OSError as e:
        return {"ok": False, "error": f"删除失败: {e}"}
    return {"ok": True, "files_removed": removed}


# ---- mcp settings ----
@app.get("/api/mcp/config")
async def get_mcp_config_endpoint() -> dict:
    p = paths.mcp_config_path()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text() or '{"mcpServers": {}}')
    except json.JSONDecodeError:
        return {"mcpServers": {}}


@app.put("/api/mcp")
async def put_mcp_endpoint(data: dict) -> dict:
    paths.mcp_config_path().write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/mcp/reload")
async def reload_mcp_endpoint() -> dict:
    global _mcp
    if _mcp:
        await _mcp.close_all()
    _mcp = MCPRegistry()
    _mcp.load()
    await _mcp.connect_all()
    return {"ok": True, "servers": list(_mcp.ensure_loaded().keys())}


# ---- skills settings ----
@app.get("/api/skills/{name}/body")
async def get_skill_body(name: str, project_slug: str | None = None) -> dict:
    s = SkillLoader(project_slug=project_slug).get(name)
    return {"ok": bool(s), "body": s.body if s else ""}


@app.post("/api/skills")
async def create_skill_endpoint(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    body = data.get("body") or ""
    if not name:
        return {"ok": False, "error": "name required"}
    d = paths.global_skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    await _push_global_event("skills.changed", {})
    return {"ok": True}


@app.delete("/api/skills/{name}")
async def delete_skill_endpoint(name: str) -> dict:
    import shutil

    d = paths.global_skills_dir() / name
    if d.exists():
        shutil.rmtree(d)
        await _push_global_event("skills.changed", {})
        return {"ok": True}
    s = SkillLoader().get(name)
    if s and s.builtin:
        return {"ok": False, "error": "builtin skill cannot be deleted"}
    return {"ok": False}


@app.post("/api/skills/import-dir")
async def import_skills_dir(data: dict) -> dict:
    """Import skills from a local directory (e.g. another agent's skills folder).

    Each sub-directory containing a ``SKILL.md`` (or lowercase ``skill.md``) is
    imported as one skill; the whole sub-directory (scripts, reference docs,
    mcp-config, etc.) is copied so script-backed skills keep working. If *path*
    itself is a single skill directory, only that one is imported. Existing
    skills are skipped unless ``overwrite`` is true.

    Shares its implementation with the agent-side ``install_skills`` tool
    (:mod:`ginno_runtime.skills.installer`).
    """
    from .skills.installer import import_skills_from_dir

    result = import_skills_from_dir(
        (data or {}).get("path", ""),
        overwrite=bool((data or {}).get("overwrite", False)),
    )
    if result.get("ok") and result.get("imported"):
        await _push_global_event("skills.changed", {})
    return result


# ---- knowledge base (via MCP vault servers) ----
@app.get("/api/kb/servers")
async def kb_servers_endpoint() -> list[dict]:
    if not _mcp:
        return []
    return [
        {"name": n, "tools": [t.name for t in live.tools]}
        for n, live in _mcp._live.items()
    ]


def _server_roots(name: str) -> list[str]:
    """Best-effort root path(s) for a server, read from mcp.json (the filesystem
    server takes its allowed directory as a positional arg)."""
    p = paths.mcp_config_path()
    if not p.exists():
        return []
    try:
        cfg = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    srv = (cfg.get("mcpServers") or {}).get(name, {}) or {}
    return [a for a in (srv.get("args") or []) if isinstance(a, str) and a.startswith("/")]


async def _kb_call_one(live, tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not live.session or not any(t.name == tool_name for t in live.tools):
        return out
    try:
        res = await live.session.call_tool(tool_name, args)
        for c in getattr(res, "content", []) or []:
            t = getattr(c, "text", None)
            if t:
                out.append(t)
    except Exception:
        pass
    return out


async def _kb_call(tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not _mcp:
        return out
    for live in _mcp._live.values():
        out.extend(await _kb_call_one(live, tool_name, args))
    return out


@app.get("/api/kb/search")
async def kb_search_endpoint(q: str = "") -> dict:
    if not q or not _mcp:
        return {"q": q, "results": []}
    results: list[str] = []
    for name, live in _mcp._live.items():
        for root in _server_roots(name) or [""]:
            results.extend(await _kb_call_one(live, "search_files", {"path": root, "pattern": q}))
    return {"q": q, "results": results}


@app.get("/api/kb/list")
async def kb_list_endpoint(path: str = "") -> dict:
    if not _mcp:
        return {"path": path, "results": []}
    results: list[str] = []
    for name, live in _mcp._live.items():
        roots = [path] if path else (_server_roots(name) or [""])
        for root in roots:
            r = await _kb_call_one(live, "list_directory", {"path": root}) or await _kb_call_one(
                live, "directory_tree", {"path": root}
            )
            results.extend(r)
    return {"path": path, "results": results}


# ---- knowledge base / LLMWiki (in-memory vault index + retrieval) ----
from .knowledge.config import load_knowledge_config as _load_kb_cfg
from .knowledge.indexer import get_indexer as _get_kb_indexer
from .knowledge.retriever import WikiRetriever as _WikiRetriever
from .knowledge import compiler as _kb_compiler
from .knowledge.association import get_engine as _get_kb_engine, reset_engines as _reset_kb_engines
from .knowledge.semantic import get_semantic_index as _get_kb_semantic, reset_semantic as _reset_kb_semantic


def _kb_not_configured(extra: dict | None = None) -> dict:
    return {"ok": False, "error": "knowledge not configured", **(extra or {})}


def _kb_indexer(cfg):
    """Shared indexer over the whole vault minus the raw sources dir and system
    dirs (``SKIP_DIRS`` such as ``.obsidian``). Finished notes anywhere in the
    vault (e.g. a ``股市/`` folder) are searchable and visible; ``raw_dir`` holds
    compile-sources that surface through their compiled wiki pages instead. An
    empty ``raw_dir`` excludes nothing extra. (The compiler's INDEX/association
    graph deliberately stays scoped to ``wiki_dir`` — see compiler.py.)"""
    return _get_kb_indexer(
        cfg.vault_path,
        cfg.rescan_interval_s,
        exclude_dirs=[cfg.raw_dir] if cfg.raw_dir else None,
    )


def _count_md(root) -> int:
    import os

    from pathlib import Path as _P

    from .knowledge.indexer import SKIP_DIRS, INDEX_EXTENSIONS

    if not root.exists():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if _P(fn).suffix.lower() in INDEX_EXTENSIONS:
                n += 1
    return n


def _detect_wiki_layout(vault) -> dict:
    """Find a `<namespace>/Wiki` (or a root `Wiki`) layout in *vault*."""
    from pathlib import Path as _P

    def _sister(ns_dir, name):
        d = (ns_dir / name) if ns_dir else (vault / name)
        return d.relative_to(vault).as_posix() if d.is_dir() else ""

    root_wiki = vault / "Wiki"
    if root_wiki.is_dir():
        ns_dir = None
        namespace = ""
    else:
        ns_dir = None
        namespace = ""
        for child in sorted(vault.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / "Wiki").is_dir():
                    ns_dir = child
                    namespace = child.name
                    break
    wiki_dir = _sister(ns_dir, "Wiki")
    return {
        "namespace": namespace,
        "wiki_dir": wiki_dir,
        "raw_dir": _sister(ns_dir, "Raw"),
        "research_dir": _sister(ns_dir, "Research"),
        "memory_dir": _sister(ns_dir, "Memory"),
        "todo_dir": _sister(ns_dir, "Todo"),
    }


@app.get("/api/kb/wiki/probe")
def kb_wiki_probe(path: str = "") -> dict:
    """Read-only: detect an existing LLM-Wiki layout under *path* and count pages.

    Does NOT write the vault. Used by the import UI to pre-fill config and show
    how many compiled wiki pages / raw docs a vault contains.
    """
    from pathlib import Path as _P

    if not path:
        return {"ok": False, "error": "path required"}
    vault = _P(path).expanduser().resolve()
    if not vault.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    layout = _detect_wiki_layout(vault)
    wiki_abs = (vault / layout["wiki_dir"]) if layout["wiki_dir"] else vault
    raw_abs = (vault / layout["raw_dir"]) if layout["raw_dir"] else None
    return {
        "ok": True,
        "vault_path": str(vault),
        "detected": layout,
        "wiki_pages": _count_md(wiki_abs),
        "raw_pages": _count_md(raw_abs) if raw_abs else 0,
        "has_index": (wiki_abs / "INDEX.md").is_file() if layout["wiki_dir"] else False,
        "total_md": _count_md(vault),
    }


# ---- memory summarization (P2) ----
@app.get("/api/memory")
async def get_memory() -> dict:
    """Return MEMORY.md content + pool count."""
    from .memory import pool_count

    p = paths.memory_index_path()
    content = p.read_text(encoding="utf-8") if p.exists() else ""
    return {"ok": True, "content": content, "pool_count": pool_count()}


@app.post("/api/memory/summarize")
async def post_memory_summarize(data: dict | None = None) -> dict:
    """Trigger memory summarization (pool → MEMORY.md via LLM)."""
    from .memory import summarize_pool

    provider = (data or {}).get("provider")
    return await summarize_pool(model_provider=provider)


@app.get("/api/kb/wiki/search")
async def kb_wiki_search(q: str = "", tag: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"results": []})
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    ret = _WikiRetriever(entries)
    if tag:
        results = ret.search_by_tag(tag)
    else:
        results = ret.retrieve(
            q,
            top_k=10,
            min_score=0.2,
            semantic=_get_kb_semantic(cfg, entries),
            semantic_weight=cfg.semantic_weight,
        )
    return {"ok": True, "q": q, "tag": tag, "results": [r.to_dict() for r in results]}


@app.get("/api/kb/wiki/list")
async def kb_wiki_list() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [
        {
            "title": e.title,
            "path": e.relative_path,
            "tags": e.tags,
            "links": e.links,
            "modified": e.modified,
        }
        for e in idx.get_entries()
    ]
    return {"ok": True, "pages": pages}


@app.get("/api/kb/wiki/stats")
async def kb_wiki_stats() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    by_dir: dict[str, int] = {}
    for e in entries:
        top = e.relative_path.split("/", 1)[0] if "/" in e.relative_path else "(root)"
        by_dir[top] = by_dir.get(top, 0) + 1
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    unique_tags = sorted(tag_counts, key=lambda t: (-tag_counts[t], t))[:30]
    return {
        "ok": True,
        "vault_path": cfg.vault_path,
        "total_pages": len(entries),
        "pages_by_dir": by_dir,
        "total_links": sum(len(e.links) for e in entries),
        "total_tags": len(tag_counts),
        "unique_tags": unique_tags,
        "last_indexed": idx.last_full_scan,
    }


def _vault_resolve(cfg, rel: str):
    """Resolve a vault-relative (or absolute) path and ensure it stays inside the
    vault. Returns the resolved Path or None when it escapes the vault."""
    from pathlib import Path as _P

    vault = _P(cfg.vault_path).expanduser().resolve()
    p = _P(rel)
    p = (vault / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        return None
    return p


@app.get("/api/kb/wiki/page")
def kb_wiki_page(path: str = "", title: str = "") -> dict:
    """Read one vault note in full (raw text incl. frontmatter) for the preview /
    editor. Resolves by ``path`` (vault-relative) or, failing that, by ``title``
    via the index. A note that doesn't exist yet returns ``exists:false`` so the
    UI can offer to create it (Obsidian-style click-on-dangling-wikilink)."""
    from .knowledge import frontmatter as _fm

    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    vault = _Path(cfg.vault_path).expanduser().resolve()
    p = _vault_resolve(cfg, path) if path else None
    if p is None or not p.exists():
        # fall back to title → indexed page
        if title:
            ent = _kb_indexer(cfg).find_by_title(title)
            if ent:
                p = _Path(ent.path)
    if p is None or not p.exists():
        # dangling link: surface a create-able stub
        stub_title = title or (_Path(path).stem if path else "")
        return {
            "ok": True,
            "exists": False,
            "path": path,
            "title": stub_title,
            "tags": [],
            "links": [],
            "raw": "",
        }
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    meta, body = _fm.split_frontmatter(raw)
    rel = p.resolve().relative_to(vault).as_posix()
    return {
        "ok": True,
        "exists": True,
        "path": rel,
        "title": (meta.get("title") or "").strip() or _fm.extract_title(body) or p.stem,
        "tags": _fm._as_list(meta.get("tags")),
        "links": _fm.extract_wikilinks(body),
        "raw": raw,
    }


@app.put("/api/kb/wiki/page")
def kb_wiki_page_put(data: dict) -> dict:
    """Write a note's full raw text back to the vault (path must stay in-vault and
    end in .md), then refresh the index so the preview/list/graph update."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    rel = (data or {}).get("path", "")
    raw = (data or {}).get("raw", "")
    if not rel or not str(rel).lower().endswith((".md", ".markdown")):
        return {"ok": False, "error": "path must be a .md file"}
    p = _vault_resolve(cfg, rel)
    if p is None:
        return {"ok": False, "error": "path outside vault"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw if isinstance(raw, str) else "", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, "path": rel}


@app.post("/api/kb/wiki/page")
def kb_wiki_page_post(data: dict) -> dict:
    """Create a new note (fails if it already exists — use PUT to overwrite)."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    rel = (data or {}).get("path", "")
    raw = (data or {}).get("raw", "")
    if not rel or not str(rel).lower().endswith((".md", ".markdown")):
        return {"ok": False, "error": "path must be a .md file"}
    p = _vault_resolve(cfg, rel)
    if p is None:
        return {"ok": False, "error": "path outside vault"}
    if p.exists():
        return {"ok": False, "error": "already exists"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw if isinstance(raw, str) else "", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, "path": rel}


@app.post("/api/kb/wiki/index")
def kb_wiki_index() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    n = idx.scan()
    _maybe_build_semantic(cfg)
    return {"ok": True, "indexed": n, "tags": idx.get_all_tags()}


def _kb_refresh(cfg) -> None:
    """Force the shared indexer to rescan and drop the cached association graph."""
    _kb_indexer(cfg).scan()
    _reset_kb_engines()


def _maybe_build_semantic(cfg) -> None:
    """Encode wiki pages into the semantic index after a build/reindex (no-op
    unless ``use_semantic`` is on). Failures are swallowed → lexical fallback."""
    if not getattr(cfg, "use_semantic", False):
        return
    try:
        _get_kb_semantic(cfg, _kb_indexer(cfg).get_entries(), build=True)
    except Exception:  # noqa: BLE001
        pass


def _compile_to_dict(res) -> dict:
    return {
        "created": res.created,
        "updated": res.updated,
        "new_links": res.new_links,
        "discovered": res.discovered,
    }


@app.post("/api/kb/wiki/ingest")
def kb_wiki_ingest(data: dict) -> dict:
    """Compile a single raw file (path absolute or relative to the vault)."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    from pathlib import Path as _P

    vault = _P(cfg.vault_path).resolve()
    raw = (data or {}).get("path", "")
    p = _P(raw) if _P(raw).is_absolute() else (vault / raw).resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        return {"ok": False, "error": "path outside vault"}
    if not p.is_file():
        # compile() silently no-ops on a missing path and returned ok:True with
        # empty created/updated — callers couldn't tell failure from empty.
        return {"ok": False, "error": "file not found"}
    comp = _kb_compiler.WikiCompiler(vault, cfg.wiki_dir, cfg.raw_dir)
    res = comp.compile(p)
    comp.update_index()
    _kb_refresh(cfg)
    return {"ok": True, **_compile_to_dict(res)}


@app.post("/api/kb/wiki/build")
def kb_wiki_build() -> dict:
    """Compile every raw file in the vault (raw→wiki) and rebuild the index."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    from pathlib import Path as _P

    comp = _kb_compiler.WikiCompiler(_P(cfg.vault_path), cfg.wiki_dir, cfg.raw_dir)
    result = comp.build_all()
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, **result}


@app.get("/api/kb/wiki/related")
def kb_wiki_related(title: str = "", top_k: int = 10) -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"related": [], "clusters": []})
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).find_related(title, top_k=top_k)}


@app.get("/api/kb/wiki/discover")
def kb_wiki_discover() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).discover()}


@app.get("/api/kb/wiki/orphans")
def kb_wiki_orphans() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [{"title": e.title, "path": e.relative_path, "tags": e.tags} for e in idx.get_orphans()]
    return {"ok": True, "pages": pages}


@app.get("/api/kb/wiki/backlinks")
def kb_wiki_backlinks(title: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"backlinks": []})
    idx = _kb_indexer(cfg)
    bl = idx.get_backlinks(title)
    return {"ok": True, "title": title, "backlinks": bl, "count": len(bl)}


@app.put("/api/kb/wiki/config")
async def kb_wiki_put_config(data: dict) -> dict:
    from dataclasses import fields as _fields

    from .knowledge.config import save_knowledge_config as _save_kb_cfg
    from .knowledge.types import KnowledgeConfig as _KC

    current = _load_kb_cfg()
    known = {f.name for f in _fields(_KC)}
    merged = {**current.__dict__, **{k: v for k, v in data.items() if k in known}}
    cfg = _KC(**merged)
    _save_kb_cfg(cfg)
    from .knowledge.indexer import reset_indexers as _reset_kb

    _reset_kb()  # pick up a changed vault_path on the next call
    _reset_kb_semantic()  # embeddings are keyed by vault path
    return {"ok": True, "config": merged}


def _first_agent_id() -> str | None:
    lst = agents_reg.list_agents()
    return lst[0].id if lst else None


def _find_meta(session_id: str) -> tuple[dict, str] | None:
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m, slug
    return None


def _ensure_session(session_id: str) -> dict[str, Any] | None:
    """Return the in-memory session, lazily rebuilding its graph from the
    on-disk index meta when the runtime was restarted (so sessions survive
    restarts; history is restored by the file checkpointer via thread_id)."""
    s = _SESSIONS.get(session_id)
    if s:
        return s
    found = _find_meta(session_id)
    if not found:
        return None
    meta, slug = found
    provider = meta.get("provider") or prov_mod.get_default_provider()
    model_name = meta.get("model") or prov_mod.model_for_provider(
        prov_mod.load_providers(), provider
    )
    try:
        model = build_model(provider, model_name)
    except ValueError:
        return None
    # Derive the workspace from paths (not meta) so legacy sessions whose meta
    # still holds the shared "/tmp/gw" converge on the per-session dir without
    # rewriting the index.
    workspace = str(paths.session_files_dir(slug, session_id))
    mcp_tools = _mcp.all_langchain_tools() if _mcp else []
    all_tools = build_all_tools(
        mcp_tools, workspace=workspace, project_slug=slug, session_id=session_id
    )
    graph = build_graph(
        model=model,
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=_hooks,
        all_tools=all_tools,
    )
    s = {
        "session_id": session_id,
        "project_slug": slug,
        "workspace": workspace,
        "agent_id": meta.get("agent_id"),
        "title": meta.get("title"),
        "icon": meta.get("icon"),
        "model_provider": provider,
        "model_name": model_name,
        "graph": graph,
        "model": model,
        "all_tool_names": [t.name for t in all_tools],
        "mcp_tool_names": [t.name for t in mcp_tools],
    }
    _SESSIONS[session_id] = s
    return s


@app.websocket("/api/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    session = _ensure_session(session_id)
    if not session:
        await ws.accept()
        await ws.send_text(_ev("error", {"message": f"unknown session: {session_id}"}))
        await ws.close()
        return

    await ws.accept()
    _SESSION_WS.setdefault(session_id, []).append(ws)
    _log.info("ws_open session=%s agent=%s", session_id, session.get("agent_id"))
    graph = session["graph"]
    config = {"configurable": {"thread_id": session_id, "project_slug": session["project_slug"]}}

    # File watcher (docs §7.5): stat the session's registered files every 5s.
    # A changed mtime → preview.invalidate (the UI refreshes if that file is
    # open) + a stale badge on the artifact (cleared on next preview fetch).
    # Sends share _ws_lock so watcher frames never interleave with turn frames.
    _ws_lock = asyncio.Lock()
    _watch_stop = asyncio.Event()

    async def _file_watcher() -> None:
        reg = files_mod.get_registry(session["project_slug"])
        while not _watch_stop.is_set():
            try:
                await asyncio.wait_for(_watch_stop.wait(), timeout=5)
                break  # stop set
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            artifacts_dirty = False
            try:
                for e in reg.list_session(session_id):
                    try:
                        m = Path(e["path"]).stat().st_mtime
                    except OSError:
                        continue
                    if m != e.get("mtime", 0):
                        e["mtime"] = m
                        if not e.get("stale"):
                            reg.mark_stale(e["id"], True)
                            artifacts_dirty = True
                        async with _ws_lock:
                            await ws.send_text(
                                _ev(
                                    "preview.invalidate",
                                    {"file_id": e["id"], "reason": "mtime"},
                                )
                            )
                if artifacts_dirty:
                    async with _ws_lock:
                        await ws.send_text(_ev("artifacts.changed", {}))
            except Exception:
                continue

    _watcher_task = asyncio.create_task(_file_watcher())

    # Re-emit any permission interrupt left pending from a previous connection:
    # the graph pauses at permission_node awaiting a resume, so a reconnect or a
    # session switch mid-permission would otherwise orphan the turn (the prompt
    # is gone and there is no way to resume). The payload mirrors the
    # __interrupt__ handling in _run_stream below, so the client's existing
    # permission.request handler applies unchanged.
    try:
        snap = await graph.aget_state(config)
        for task in getattr(snap, "tasks", []) or []:
            for intr in getattr(task, "interrupts", []) or []:
                value = getattr(intr, "value", None) or intr
                if isinstance(value, dict) and value.get("kind") == "permission_request":
                    # Re-arm the resume guard: after a runtime restart the
                    # in-memory flag is gone even though the interrupt persists.
                    _PENDING_RESUME.add(session_id)
                    _RUNNING_TURNS.setdefault(session_id, "")
                    await ws.send_text(
                        _ev(
                            "permission.request",
                            {"tool": value.get("tool"), "args": value.get("args")},
                        )
                    )
                elif isinstance(value, dict) and value.get("kind") == "version_propose":
                    # a workflow-dev diff proposal pending at disconnect: re-show it
                    # so the turn isn't orphaned (same resume channel as permission).
                    _PENDING_RESUME.add(session_id)
                    _RUNNING_TURNS.setdefault(session_id, "")
                    await ws.send_text(
                        _ev(
                            "version.propose",
                            {
                                "workflow_id": value.get("workflow_id"),
                                "from_version": value.get("from_version"),
                                "diff": value.get("diff"),
                                "rationale": value.get("rationale"),
                            },
                        )
                    )
    except Exception:
        # introspecting resume state must never stop the socket from opening
        pass

    # Cross-restart goal resume (design §4.3.3): an active goal re-arms its
    # continuation driver as soon as the session is loaded again.
    _start_goal_driver(session_id)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(_ev("error", {"message": "invalid JSON"}))
                continue

            kind = msg.get("type")
            if kind == "invoke":
                try:
                    user_text = msg.get("message", "")
                    # Per-turn trace id: prefer the client-supplied one (so the UUID
                    # shown on the user bubble matches the one we log + put on every
                    # event), else mint one. Forward it via config so _stream_graph
                    # tags every emitted event + log line with it.
                    turn_id = msg.get("turn_id") or str(uuid.uuid4())
                    _imgs = msg.get("images") or []
                    _log.info(
                        "invoke session=%s turn=%s agent=%s imgs=%d text=%r",
                        session_id,
                        turn_id,
                        msg.get("agent_id") or session.get("agent_id"),
                        len([i for i in _imgs if isinstance(i, dict) and i.get("data")]),
                        (user_text or "")[:120],
                    )
                    # Slash commands + @mentions → TurnPlan (docs §commands).
                    plan = _commands.resolve_turn(msg, session)
                    if plan.builtin_reply is not None:
                        # Built-in command: reply directly, no graph turn, no agent
                        # persistence, no checkpoint write (ephemeral by design).
                        await ws.send_text(
                            _ev("notice", {"message": plan.builtin_reply}, turn_id)
                        )
                        await ws.send_text(_ev("message.end", {}, turn_id))
                        continue
                    user_text = plan.text
                    turn_agent = (
                        plan.agent_override
                        or msg.get("agent_id")
                        or session.get("agent_id")
                        or _first_agent_id()
                    )
                    if turn_agent != session.get("agent_id"):
                        session["agent_id"] = turn_agent
                        _session_meta_patch(
                            session["project_slug"], session_id, {"agent_id": turn_agent}
                        )
                    turn_config = {
                        **config,
                        "configurable": {
                            **config["configurable"],
                            "agent_id": turn_agent,
                            "turn_id": turn_id,
                            "user_text": user_text or "",
                        },
                    }
                    async with _turn_lock(session_id):
                        await _run_stream(
                            ws,
                            graph,
                            turn_config,
                            user_text,
                            session,
                            turn_agent,
                            images=msg.get("images"),
                            files=(msg.get("files") or []) + plan.files_extra,
                            mention_context=plan.mention_ctx,
                            skill_name=plan.skill_name,
                        )
                    # The turn may have created/resumed a goal (goal tools) —
                    # (re)arm the continuation driver now that we're idle.
                    _start_goal_driver(session_id)
                except Exception as e:
                    # Any lower-layer failure on the invoke path (command /
                    # mention resolution, stream setup, graph run leaking past
                    # its own handler) surfaces as an in-chat error card instead
                    # of a silently dropped socket.
                    _log.exception("invoke_error session=%s", session_id)
                    try:
                        await ws.send_text(
                            _ev("error", {"message": f"{type(e).__name__}: {e}"})
                        )
                    except Exception:
                        return  # socket died while reporting; nothing to do
            elif kind == "permission_response":
                # The prompt broadcasts to every socket of the session (tabs),
                # so a second response can arrive after the first already
                # resumed the graph — ignore it instead of double-resuming.
                if session_id not in _PENDING_RESUME:
                    continue
                _PENDING_RESUME.discard(session_id)
                decision = msg.get("decision", "deny")
                # resume under the agent that was active when the interrupt fired
                resume_agent = session.get("agent_id") or _first_agent_id()
                resume_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "agent_id": resume_agent,
                    },
                }
                await _run_resume(ws, graph, resume_config, {"decision": decision})
            elif kind == "turn_state":
                # Post-reconnect probe (frontend ChatStream): is a turn still
                # streaming (or parked at an interrupt) for this session? If
                # not, the client reconciles against /history instead of
                # waiting on a stream that will never resume.
                try:
                    await ws.send_text(
                        _ev(
                            "turn.state",
                            {
                                "running": session_id in _RUNNING_TURNS,
                                "turn_id": _RUNNING_TURNS.get(session_id, ""),
                            },
                        )
                    )
                except Exception:
                    return  # socket died between recv and send
            elif kind == "ping":
                try:
                    await ws.send_text(_ev("pong", {}))
                except Exception:
                    return  # socket died between recv and send
            else:
                try:
                    await ws.send_text(_ev("error", {"message": f"unknown type: {kind}"}))
                except Exception:
                    return
    except WebSocketDisconnect:
        return
    finally:
        _log.info("ws_close session=%s", session_id)
        _watch_stop.set()
        _watcher_task.cancel()
        # Drop this socket from the broadcast registry (a dead entry would
        # otherwise linger until the next send attempt pruned it).
        _SESSION_WS[session_id] = [
            w for w in (_SESSION_WS.get(session_id) or []) if w is not ws
        ]


def _compact_schema(path: str) -> str:
    """One-line schema summary for prompt injection (tables only, best-effort)."""
    try:
        from .files import extractors as _ex

        s = _ex.schema_summary(path, sample_rows=2)
        bits = []
        for sh in s.get("sheets", [])[:3]:
            cols = ", ".join(
                f"{c['name']}({c['dtype']})" for c in sh.get("columns", [])[:12]
            )
            bits.append(
                f"[{sh['name']}] {sh['rows']}行×{sh['cols']}列, 列: {cols}"
                + (f", 样例: {sh['sample']}" if sh.get("sample") else "")
            )
        return "; ".join(bits)[:500]
    except Exception:
        return ""


def _resolve_attached_files(
    files: list | None, slug: str, session_id: str
) -> list[dict]:
    """Turn invoke ``files`` items ({id} or {artifact_id} or {name, path})
    into registry-backed entries carrying a compact schema for table kinds."""
    from .files import extractors as _ex

    reg = files_mod.get_registry(slug)
    out: list[dict] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        entry = None
        if f.get("id"):
            entry = files_mod.get_by_id(f["id"])
        # @artifact mention: resolve the artifact's own file ref. Never call
        # add_artifact here — the artifact already exists, and re-adding under
        # a hardcoded kind would duplicate its right-panel row.
        if entry is None and f.get("artifact_id"):
            art = art_store.get_artifact(slug, f["artifact_id"])
            ref = (art.get("ref") or "").strip() if art else ""
            if art and ref:
                p = Path(ref).expanduser()
                if p.is_file():
                    entry = reg.find_by_path(str(p)) or reg.register(
                        art.get("name") or p.name,
                        p,
                        session_id=session_id,
                        artifact_id=art.get("id"),
                    )
        if entry is None and f.get("path"):
            p = Path(f["path"]).expanduser()
            if p.is_file():
                art = art_store.add_artifact(
                    slug, "file", f.get("name") or p.name, str(p), session_id
                )
                entry = reg.register(
                    f.get("name") or p.name, p, session_id=session_id, artifact_id=art.get("id")
                )
        if entry is None:
            continue
        item = {
            "id": entry["id"],
            "name": entry["name"],
            "path": entry["path"],
            "kind": entry.get("kind") or _ex.classify(entry["path"]),
        }
        # A user-corrected schema (set via the metadata inspector) wins over
        # the auto-computed one — that's the whole point of allowing edits.
        override = ""
        aid = entry.get("artifact_id")
        if aid:
            art = art_store.get_artifact(slug, aid)
            override = ((art.get("schema") or "") if art else "").strip()
        if override:
            item["schema"] = override
        elif item["kind"] in ("spreadsheet", "table"):
            item["schema"] = _compact_schema(entry["path"])
        out.append(item)
    return out


async def _tool_file_effects(
    safe_send, emit, slug: str, session_id: str, name_args: tuple[str, dict] | None, content: str
) -> None:
    """After a tool finishes, keep file previews live (docs §7.5):

    1. Structured tools declare their path arg → ``registry.touch`` → the UI
       gets ``preview.invalidate`` for that file.
    2. Opaque tools (bash / MCP) → best-effort: any registered path appearing
       in the tool args is touched.
    3. ``analyze_table`` table results → register the derived CSV as an
       artifact and emit ``preview.emit {open: true}`` so the result sheet
       opens automatically in the UI.
    """
    if not slug or not name_args:
        return
    name, args = name_args
    reg = files_mod.get_registry(slug)
    touched: list[str] = []

    if name in ("write_file", "edit_file", "read_file", "parse_document", "analyze_table"):
        p = (args or {}).get("path")
        if p:
            touched.append(str(Path(p).expanduser().resolve()))
    elif args:
        try:
            blob = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            blob = str(args)
        for e in reg.list_session(session_id) or reg.list_all():
            if e.get("path") and e["path"] in blob:
                touched.append(e["path"])

    if name == "analyze_table" and content:
        try:
            d = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            d = None
        dp = d.get("derived_path") if isinstance(d, dict) and d.get("ok") else None
        if dp and Path(dp).is_file():
            # Relocate the derived CSV into the session's results/ dir so every
            # session artifact lives under sessions/<sid>/ — the tool writes it
            # next to the source file, which may sit outside the session dir
            # (e.g. an external path the user analyzed). Happens in-turn, before
            # registration, so the artifact is stored at its final path.
            import shutil

            results_dir = paths.session_results_dir(slug, session_id)
            try:
                results_dir.mkdir(parents=True, exist_ok=True)
                final = files_mod.unique_dest(results_dir / Path(dp).name)
                shutil.move(str(dp), str(final))
                dp = str(final)
            except OSError:
                pass  # keep the tool's original location if the move fails
            norm_ref = files_mod.norm_path(dp)
            art = art_store.add_artifact(slug, "file", Path(dp).name, norm_ref, session_id)
            entry = reg.register(
                Path(dp).name, dp, kind="table", session_id=session_id, artifact_id=art.get("id")
            )
            await safe_send(
                emit(
                    "preview.emit",
                    {
                        "file_id": entry["id"],
                        "name": entry["name"],
                        "path": entry["path"],
                        "kind": "table",
                        "open": True,
                    },
                )
            )
            await safe_send(emit("artifacts.changed", {}))
            touched.append(norm_ref)

    seen: set[str] = set()
    for p in touched:
        if p in seen:
            continue
        seen.add(p)
        for e in files_mod.touch(p, reason=f"tool:{name}"):
            await safe_send(
                emit("preview.invalidate", {"file_id": e["id"], "reason": f"tool:{name}"})
            )


async def _run_stream(
    ws: WebSocket,
    graph,
    config: dict,
    user_text: str,
    session: dict,
    agent_id: str | None = None,
    images: list | None = None,
    files: list | None = None,
    mention_context: list | None = None,
    skill_name: str | None = None,
) -> None:
    """Append a HumanMessage and stream the agent loop until end or interrupt.

    ``images`` carries ``{"data": <base64>, "media_type": "image/png"}`` items
    from the composer; when present the HumanMessage becomes a multimodal
    content list (OpenAI-style image_url data URLs, which both ChatOpenAI and
    ChatAnthropic accept). ``files`` carries uploaded file refs ({id} or
    {name, path}) resolved via the file registry and injected into the system
    prompt through ``state["attached_files"]``. ``mention_context`` carries
    resolved @mention sections (workflow/memory/non-file artifact) injected
    into the system prompt; ``skill_name`` marks an invoked slash-skill turn.
    """
    content: Any = user_text
    imgs = [i for i in (images or []) if isinstance(i, dict) and i.get("data")]
    if imgs:
        parts: list[dict] = []
        if user_text:
            parts.append({"type": "text", "text": user_text})
        for img in imgs:
            media = img.get("media_type") or "image/png"
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{img['data']}"}}
            )
        content = parts
    attached = _resolve_attached_files(
        files, session["project_slug"], session.get("session_id", "")
    )
    if attached and not (user_text or "").strip():
        # Drop with no text: synthesize a default intent so the turn still runs.
        content = "请概览我附加的文件：结构、数据质量与关键指标，并给出简短结论。"

    session_id = session.get("session_id", "")
    slug = session["project_slug"]
    turn_id = ((config or {}).get("configurable") or {}).get("turn_id")
    effective_agent = agent_id or session.get("agent_id") or ""

    _live_names: dict[str, list[str]] = {}

    def _world_ctx() -> SessionCtx:
        # Live tool/MCP name lists. The session dict freezes these at graph
        # build time — when MCP may still be connecting — which made the world
        # diff oscillate (24→64 / 0→40) and announce phantom changes on every
        # turn. Recompute from the live registries instead (cheap: object
        # construction only, memoized per invoke).
        if not _live_names:
            mcp_tools = _mcp.all_langchain_tools() if _mcp else []
            _live_names["mcp"] = [t.name for t in mcp_tools]
            _live_names["all"] = [
                t.name
                for t in build_all_tools(
                    mcp_tools,
                    workspace=str(session.get("workspace") or ""),
                    project_slug=slug,
                    session_id=session_id,
                )
            ]
        return SessionCtx(
            session_id=session_id,
            project_slug=slug,
            agent_id=effective_agent or None,
            mcp_tool_names=list(_live_names["mcp"]),
            all_tool_names=list(_live_names["all"]),
            workspace=str(session.get("workspace") or ""),
        )

    # Microcompact — clear stale tool outputs (rung below E3) BEFORE E3
    # measures tokens: if clearing frees enough, the full summary never fires.
    # Pure state rewrite, no LLM call. Same never-a-blocker contract.
    microcompact_stats = None
    try:
        from .microcompact import maybe_microcompact_history

        microcompact_stats = await maybe_microcompact_history(session, config)
    except Exception:
        _log.exception("microcompact_failed session=%s", session_id)
    if microcompact_stats:
        await _push_session_event(
            session_id,
            "context.microcompacted",
            {
                "cleared_tool_outputs": microcompact_stats["cleared_tool_outputs"],
                "chars_freed": microcompact_stats["chars_freed"],
            },
            turn_id,
        )

    # E3 — history compaction, checked BEFORE this turn's messages land.
    # Never fires while an interrupt is pending (guarded inside). Failures are
    # logged and swallowed: compaction is an optimization, never a blocker.
    compaction_stats = None
    try:
        from .compaction import maybe_compact_history

        compaction_stats = await maybe_compact_history(session, config, ctx_factory=_world_ctx)
    except Exception:
        _log.exception("compaction_failed session=%s", session_id)
    if compaction_stats:
        await _push_session_event(
            session_id,
            "context.compacted",
            {
                "compacted_messages": compaction_stats["compacted_messages"],
                "kept_messages": compaction_stats["kept_messages"],
            },
            turn_id,
        )

    # C1/C2 — WorldState diff against the session baseline. First sync only
    # records the baseline (the initial system prompt already carries the
    # world); later syncs may yield ONE merged update message + chip event.
    update_text = None
    if context_settings().get("world_state", True):
        try:
            update_text, chip_changes = sync_world_state(_world_ctx())
        except Exception:
            _log.exception("world_state_sync_failed session=%s", session_id)
            update_text, chip_changes = None, []
        if chip_changes:
            await _push_session_event(
                session_id, "context.updated", {"changes": chip_changes}, turn_id
            )

    # B1 — per-turn volatile context (wiki retrieval / attached files /
    # @mentions) rides a tail message instead of the stable system prompt.
    turn_ctx_text = build_turn_context(
        query=user_text or "",
        attached_files=attached,
        mention_context=mention_context,
    )

    messages: list = []
    if compaction_stats and compaction_stats.get("reinject"):
        messages.append(HumanMessage(content=compaction_stats["reinject"]))  # E4
    if update_text:
        messages.append(HumanMessage(content=update_text))
    if turn_ctx_text:
        messages.append(HumanMessage(content=f"{TURN_CONTEXT_PREFIX}\n{turn_ctx_text}"))
    # The actual user message carries the turn id (origin/main retry chain
    # keys on it); the scaffolding messages above stay id-less.
    messages.append(HumanMessage(content=content, **({"id": turn_id} if turn_id else {})))

    input_state = {
        "messages": messages,
        "workspace": session["workspace"],
        "project_slug": session["project_slug"],
        "agent_id": effective_agent,
        "active_skills": [skill_name] if skill_name else [],
        "pending_tool_calls": [],
        "attached_files": attached,
        # Always present (even []) so the channel resets per turn — mentions
        # must not leak into the next turn (same last-value-wins semantics as
        # attached_files; there is no reducer on this key).
        "mention_context": mention_context or [],
        # WorldState mcp section input (A7); persists across steps like the
        # other channels, refreshed on every invoke. Live value (not the
        # session-dict freeze) so the snapshot converges with reality.
        "mcp_tool_names": list(_world_ctx().mcp_tool_names),
    }
    await _stream_graph(ws, graph, config, input_state=input_state)


async def _run_resume(ws: WebSocket, graph, config: dict, resume_value: dict) -> None:
    """Resume the graph from a pending interrupt (e.g. permission ask)."""
    await _stream_graph(ws, graph, config, command=Command(resume=resume_value))


# Max seconds between stream chunks before the stall watchdog aborts the turn
# (see chunked_stream in _stream_graph). Module-level so tests can shrink it.
CHUNK_TIMEOUT_S = 180.0


async def _stream_graph(
    ws: WebSocket,
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Drive the graph and emit token / tool / permission events."""
    # Pre-initialized so the finally-block bookkeeping below can never raise a
    # NameError that masks the original failure.
    saw_interrupt = False
    ws_closed = False
    session_id = (config.get("configurable") or {}).get("thread_id", "")
    try:
        # Per-turn trace id (from invoke, or fresh on a bare resume). `emit`
        # wraps _ev so EVERY event of this turn carries it — the frontend shows
        # it on the bubble and we log it, so a user-supplied UUID greps the logs.
        _cfg_conf = config.get("configurable") or {}
        turn_id = _cfg_conf.get("turn_id") or str(uuid.uuid4())
        _cfg_conf["turn_id"] = turn_id
        config["configurable"] = _cfg_conf

        def emit(event: str, data: dict) -> str:
            return _ev(event, data, turn_id)

        _ensure_turn_log()  # (re)point the trace file handler at the active home

        slug = (config.get("configurable") or {}).get("project_slug", "default")
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        agent_id = (config.get("configurable") or {}).get("agent_id", "")
        # Usage telemetry: continuation turns are tagged by the goal driver;
        # everything else is user-driven "chat" (usage-stats-design.md §3.6).
        usage_source = (config.get("configurable") or {}).get("usage_source") or "chat"
        _RUNNING_TURNS[session_id] = turn_id

        # The client (app webview / browser tab) can close the socket mid-turn
        # (refresh, navigate, sleep). Turn events therefore broadcast to EVERY
        # live socket of the session instead of only the invoking one: when the
        # invoke socket dies the client reconnects a fresh socket and keeps
        # receiving the running stream (2026-08-05 incident: the turn completed
        # server-side but its second half went nowhere because delivery was
        # tied to the dead socket). safe_send swallows per-socket send errors
        # and prunes dead sockets; ws_closed records whether the most recent
        # attempt found ANY live socket (diagnostic only — never latches, so a
        # reconnect mid-turn resumes delivery).
        ws_closed = False

        async def safe_send(data: str) -> None:
            nonlocal ws_closed
            socks = _SESSION_WS.get(session_id) or []
            alive: list[Any] = []
            for w in socks:
                if await _try_send(w, data):
                    alive.append(w)
            _SESSION_WS[session_id] = alive
            ws_closed = not alive

        async def keepalive() -> None:
            # The WS receive loop is sequential, so it can't answer the client's
            # app-level pings while a turn runs. If a tool/LLM step goes silent
            # for >45s the frontend's watchdog closes the socket (the "stuck at
            # 'now creating doc'" symptom). Send a harmless keepalive frame well
            # under that window so the client's lastSeen keeps resetting — even
            # while no socket is connected, so a reconnecting client never lands
            # in a >45s silent gap during a long tool step.
            try:
                while True:
                    await asyncio.sleep(15)
                    await safe_send(emit("keepalive", {}))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        _ka = asyncio.create_task(keepalive())

        # Snapshot the global TODO list before the turn: afterwards we diff it
        # to find the items the agent created/touched, and auto-link THIS
        # session to them (TODO panel → sessions association, docs "TODO 特性").
        _pre_turn_todos = todo_store.list_todos()

        if command is not None:
            stream = graph.astream(command, config=config, stream_mode=["messages", "updates"])
        else:
            stream = graph.astream(input_state, config=config, stream_mode=["messages", "updates"])
        saw_interrupt = False
        special_ids: dict[str, str] = {}  # tool_call id -> special tool name (no bubble)
        tool_args_by_id: dict[str, tuple[str, dict]] = {}  # id -> (name, args)
        turn_text: list[str] = []  # accumulate assistant text for memory capture
        # Fresh turn (not a permission resume): announce the resolved agent so the
        # UI can label the assistant bubble authoritatively (never the generic
        # "Agent" fallback).
        if command is None:
            # A new attempt supersedes any persisted last_error (empty dict =
            # cleared; _session_meta_patch skips None values).
            _session_meta_patch(slug, session_id, {"last_error": {}})
            _aid = (config.get("configurable") or {}).get("agent_id")
            _ag = agents_reg.get_agent(_aid) if _aid else None
            _log.info(
                "turn_start session=%s turn=%s agent=%s text=%r",
                session_id, turn_id, _aid,
                ((config.get("configurable") or {}).get("user_text") or "")[:120],
            )
            await safe_send(
                emit("turn.start", {"turn_id": turn_id, "agent_id": _aid or "", "name": _ag.name if _ag else "Agent"})
            )
        else:
            _log.info("turn_resume session=%s turn=%s agent=%s", session_id, turn_id, agent_id)

        # Wall-clock stall watchdog: the SDK httpx read-timeout only covers
        # *network* reads, NOT a stall inside the model generator or graph (the
        # 7m49s "stuck at 'now creating doc'" case). Wrap the stream iterator so
        # any chunk that takes longer than CHUNK_TIMEOUT_S ends the stream ->
        # the except below surfaces a fast `error` event instead of hanging.
        # A legitimately long tool call is fine because its tool-result
        # `updates` chunk resets the per-chunk clock.
        # NOTE: asyncio.wait_for is NOT used here — cancellation of the stuck
        # __anext__ can be swallowed by retry layers (they catch CancelledError
        # and keep retrying), which makes wait_for wait forever and the
        # watchdog never fires. Instead await with asyncio.wait and ABANDON the
        # stuck task on timeout (fire-and-forget cancel); at most one stuck
        # task leaks per stall, and the turn still errors out fast.
        async def chunked_stream():
            it = stream.__aiter__()
            while True:
                nxt = asyncio.ensure_future(it.__anext__())
                nxt.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )  # mark result retrieved, silence "never retrieved" warnings
                done, _ = await asyncio.wait({nxt}, timeout=CHUNK_TIMEOUT_S)
                if not done:
                    nxt.cancel()
                    # Block any late checkpoint writes from this detached run
                    # (see ABANDONED_TURNS) so it can't roll back a retry.
                    ABANDONED_TURNS.add(turn_id)
                    raise RuntimeError(
                        f"model/stream stall: no chunk for {CHUNK_TIMEOUT_S:.0f}s"
                    )
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return

        async for mode, payload in chunked_stream():
            if mode == "messages":
                chunk, msg_meta = payload
                # Only AI message chunks carry streaming text / thinking /
                # tool-call chunks. ToolMessage chunks (the result emitted by
                # the tools node, type "tool") must NOT be streamed as
                # token.delta — that would leak the tool output into the
                # assistant's text bubble (and into the memory-capture buffer).
                # Tool results reach the UI via the `updates` mode -> tool.end.
                # Streamed AI chunks report type "AIMessageChunk"; a final
                # non-streamed AIMessage reports "ai" — allow both.
                if getattr(chunk, "type", None) not in ("ai", "AIMessageChunk"):
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    for b in content:
                        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        if btype == "thinking":
                            txt = b.get("thinking") or b.get("text") or ""
                            if txt:
                                await safe_send(emit("thinking.delta", {"content": txt}))
                        elif btype == "text":
                            txt = b.get("text") or ""
                            if txt:
                                turn_text.append(txt)
                                await safe_send(emit("token.delta", {"content": txt}))
                elif isinstance(content, str) and content:
                    turn_text.append(content)
                    await safe_send(emit("token.delta", {"content": content}))
                rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if rk:
                    await safe_send(emit("thinking.delta", {"content": rk}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        if tc.get("name") and not tc.get("index") and not tc.get("args", "").strip():
                            if (
                                tc["name"] in RENDER_TOOL_NAMES
                                or tc["name"] in WORKFLOW_TOOL_NAMES
                                or tc["name"] in ARTIFACT_TOOL_NAMES
                            ):
                                special_ids[tc.get("id")] = tc["name"]
                                continue  # surfaced as widget/ref/workflow block, not a tool bubble
                            await safe_send(
                                emit("tool.start", {"name": tc["name"], "id": tc.get("id")})
                            )
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "agent":
                        for m in (delta or {}).get("messages", []):
                            # D2 — per-call usage + session accumulator. Only
                            # complete AIMessages carry usage_metadata (never
                            # streamed chunks), so this fires once per LLM call.
                            u = extract_usage(m)
                            if u:
                                acc = _USAGE_BY_SESSION.setdefault(session_id, empty_usage())
                                add_usage(acc, u)
                                # Persist the call into the global usage log
                                # (usage-stats-design.md §4). Best-effort: the
                                # store never raises, so this cannot break the
                                # turn. provider/model come from the session's
                                # meta (resolved at create/rebuild time).
                                _sreg = _SESSIONS.get(session_id) or {}
                                usage_store.record(
                                    input_tokens=u["input_tokens"],
                                    output_tokens=u["output_tokens"],
                                    cache_read_tokens=u["cache_read_tokens"],
                                    cache_creation_tokens=u["cache_creation_tokens"],
                                    provider=_sreg.get("model_provider") or "",
                                    model=_sreg.get("model_name") or "",
                                    source=usage_source,
                                    session_id=session_id or None,
                                    project_slug=slug or None,
                                    agent_id=agent_id or None,
                                    turn_id=turn_id,
                                )
                                await safe_send(
                                    emit(
                                        "usage",
                                        {
                                            "turn": u,
                                            "session": dict(acc),
                                            "cache_hit_ratio": cache_hit_ratio(acc),
                                        },
                                    )
                                )
                            for tc in getattr(m, "tool_calls", []) or []:
                                nm = tc.get("name")
                                args = tc.get("args") or {}
                                tool_args_by_id[tc.get("id")] = (nm, args)
                                if (
                                    nm in RENDER_TOOL_NAMES
                                    or nm in WORKFLOW_TOOL_NAMES
                                    or nm in ARTIFACT_TOOL_NAMES
                                ):
                                    special_ids[tc.get("id")] = nm
                                if nm == "render_widget":
                                    await safe_send(
                                        emit("widget.emit", {
                                            "kind": args.get("kind", "widget"),
                                            "data": args.get("data"),
                                        })
                                    )
                                elif nm == "attach_ref":
                                    kind = args.get("kind", "file")
                                    await safe_send(
                                        emit("ref.emit", {
                                            "kind": kind,
                                            "name": args.get("name", ""),
                                            "ref_id": args.get("ref_id", ""),
                                        })
                                    )
                                    if kind in ("file", "doc", "workflow", "link"):
                                        art_store.add_artifact(
                                            slug, kind, args.get("name", ""), args.get("ref_id", ""),
                                            session_id,
                                        )
                                elif nm == "artifact_register":
                                    art_store.add_artifact(
                                        slug,
                                        args.get("kind", "file"),
                                        args.get("name", ""),
                                        args.get("ref", ""),
                                        session_id,
                                    )
                    elif node_name == "__interrupt__":
                        items = delta if isinstance(delta, (list, tuple)) else [delta]
                        for intr in items:
                            value = getattr(intr, "value", None) or intr
                            if isinstance(value, dict) and value.get("kind") == "permission_request":
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("permission.request", {
                                        "tool": value.get("tool"),
                                        "args": value.get("args"),
                                    })
                                )
                            elif isinstance(value, dict) and value.get("kind") == "version_propose":
                                # P5: workflow edit awaiting diff confirmation.
                                # Independent of the permission system; resumed via
                                # the same permission_response WS message.
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("version.propose", {
                                        "workflow_id": value.get("workflow_id"),
                                        "from_version": value.get("from_version"),
                                        "diff": value.get("diff", ""),
                                        "rationale": value.get("rationale", ""),
                                    })
                                )
                    elif node_name == "tools":
                        msgs = (delta or {}).get("messages", [])
                        for m in msgs:
                            tc_id = getattr(m, "tool_call_id", None)
                            raw = getattr(m, "content", "") or ""
                            nm = special_ids.get(tc_id)
                            if nm in WORKFLOW_TOOL_NAMES:
                                mm = re.search(r"run_id=([0-9a-f]{6,})", raw)
                                rid = mm.group(1) if mm else None
                                run = (RUN_CACHE.get(rid) if rid else None) or (
                                    wf_store.get_run(rid) if rid else None
                                )
                                if run:
                                    # 唤起 in-session (design A): bind the chat-triggered
                                    # run to this session and drive it with the real engine.
                                    if not run.get("present_in_session_id"):
                                        run["session_id"] = session_id
                                        run["present_in_session_id"] = session_id
                                        run["updated"] = time.time()
                                        wf_storemod._write_json(wf_storemod._run_path(run["id"]), run)
                                    await safe_send(
                                        emit("run.bind", {
                                            "run_id": run["id"],
                                            "workflow_id": run["workflow_id"],
                                            "present_in_session_id": session_id,
                                        })
                                    )
                                    import asyncio as _aio

                                    t = _WF_RUN_TASKS.get(run["id"])
                                    if (t is None or t.done()) and run.get("status") == "running":
                                        _WF_RUN_TASKS[run["id"]] = _aio.create_task(
                                            _run_workflow_bg(run["id"], run["workflow_id"], None, session_id)
                                        )
                                    await safe_send(emit("workflow.emit", {"run": run}))
                                # no ordinary tool bubble for workflow tools
                            elif nm in RENDER_TOOL_NAMES or nm in ARTIFACT_TOOL_NAMES:
                                pass  # widget/ref emitted at agent-update; no bubble
                            elif tc_id:
                                await safe_send(
                                    emit("tool.end", {"id": tc_id, "content": _truncate_for_ws(_tool_content_str(raw))})
                                )
                                # File-reactive side effects (docs §7.5): invalidate
                                # previews of files this tool touched; auto-register
                                # + open derived analysis results.
                                await _tool_file_effects(
                                    safe_send, emit, slug, session_id,
                                    tool_args_by_id.get(tc_id), _tool_content_str(raw),
                                )
                    elif node_name == "permission":
                        # resolve "running" tool bubbles that were denied by
                        # tools_allow / hooks / policy / user (not streamed)
                        for m in (delta or {}).get("messages", []):
                            c = getattr(m, "content", "")
                            if isinstance(c, str) and c.startswith(BLOCK_PREFIX):
                                rest = c[len(BLOCK_PREFIX):]
                                name, _, reason = rest.partition("]")
                                await safe_send(
                                    emit("tool.end", {
                                        "name": name.strip(),
                                        "content": reason.strip() or c,
                                    })
                                )
        # Auto-associate this session with every TODO the turn created or
        # touched — the TODO panel surfaces these sessions as jump targets.
        # Gated on an actual todo_* tool call so unrelated edits don't count.
        if session_id and any(
            (nm or "").startswith("todo_") for nm, _args in tool_args_by_id.values()
        ):
            for t in todo_store.touched_since(_pre_turn_todos):
                if t.get("id") and session_id not in (t.get("session_ids") or []):
                    todo_store.link_session(t["id"], session_id)
        # refresh the right-panel TODO list after every turn (the agent may
        # have mutated it via the todo_* tools); the checkbox path is optimistic
        # and doesn't need this.
        await safe_send(emit("todos.changed", {}))
        await safe_send(emit("workflows.changed", {}))
        await safe_send(emit("artifacts.changed", {}))
        # skills too: a turn may have installed/uninstalled skills (the
        # install_skills tool — or even bash), and the slash menu / next
        # turn's skills index must reflect it without a manual refresh.
        await safe_send(emit("skills.changed", {}))
        if not saw_interrupt:
            # Capture sanitized assistant text for memory summarization (P2)
            if turn_text:
                from .memory import append_to_pool

                append_to_pool(session_id, agent_id, "".join(turn_text))
            _log.info(
                "turn_done session=%s turn=%s status=completed text_len=%d",
                session_id, turn_id, len("".join(turn_text)),
            )
            await safe_send(emit("message.end", {}))
        else:
            _log.info(
                "turn_done session=%s turn=%s status=paused_at_interrupt",
                session_id, turn_id,
            )
    except Exception as e:
        _log.exception("turn_error session=%s turn=%s", session_id, turn_id)
        err_msg = f"{type(e).__name__}: {e}"
        # Persist the failure on the session meta so the error card (with its
        # retry action) survives webview reloads and route/session switches —
        # the history endpoint re-surfaces it as the last message.
        _session_meta_patch(
            slug,
            session_id,
            {"last_error": {"turn_id": turn_id, "message": err_msg, "at": time.time()}},
        )
        await safe_send(emit("error", {"message": err_msg}))
    finally:
        try:
            _ka.cancel()
        except Exception:
            pass
        # Ended or errored (not paused at an interrupt): unregister so a client
        # `turn_state` query reports "not running". A turn parked at a
        # permission/version-propose interrupt stays registered — its resume
        # hasn't happened yet.
        if not saw_interrupt:
            _RUNNING_TURNS.pop(session_id, None)
            _PENDING_RESUME.discard(session_id)
        if ws_closed:
            _log.info(
                "turn_client_gone session=%s turn=%s (no live client socket at the "
                "last send; turn completed server-side)",
                session_id,
                turn_id,
            )


def _ev(event: str, data: dict, turn_id: str | None = None) -> str:
    if turn_id:
        data = {"turn_id": turn_id, **data}
    return json.dumps({"event": event, **data}, ensure_ascii=False, default=str)


@app.get("/{full_path:path}")
async def _serve_web(full_path: str):
    """Serve the bundled Next static export (same origin as the API)."""
    from fastapi.responses import FileResponse, HTMLResponse

    if WEB_OUT is None:
        return HTMLResponse("frontend not bundled", status_code=404)
    # HTML entry points must never be cached: they reference content-hashed
    # /_next chunks, and a heuristically-cached stale page (WKWebView) would
    # keep the webview on an old frontend build after a rebuild. The chunks
    # themselves are immutable (hashed names) and cache freely.
    no_store = {"Cache-Control": "no-store"}
    p = full_path.strip("/")
    if p == "":
        return FileResponse(WEB_OUT / "index.html", headers=no_store)
    cand = WEB_OUT / (p + ".html")
    if cand.exists():
        return FileResponse(cand, headers=no_store)
    idx = WEB_OUT / p / "index.html"
    if idx.exists():
        return FileResponse(idx, headers=no_store)
    return FileResponse(WEB_OUT / "index.html", headers=no_store)  # SPA fallback


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("GINNO_RUNTIME_PORT", "8787"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
