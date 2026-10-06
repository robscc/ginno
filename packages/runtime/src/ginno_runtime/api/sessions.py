"""Session endpoints: CRUD, goal management + the goal continuation driver,
session bootstrap (_ensure_session), and the persisted-history endpoint."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import agents as agents_reg
from .. import context_folders as cf
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from ..agents.memory import ensure_agent_memory
from ..checkpointer import FileCheckpointer
from ..goals import events as goal_events
from ..goals import store as goal_store
from ..goals import templates as goal_templates
from ..graph import build_all_tools, build_graph
from ..models import build_model
from ..server_shared import (
    _GOAL_DRIVERS,
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ..session_meta import (
    _find_meta,
    _resolve_session_meta,
    _session_meta_list,
    _session_meta_patch,
    _session_meta_remove,
    _session_meta_upsert,
    _session_slug,
    subagent_depth_of,
)
from ..subagent_scheduler import (
    SUBAGENT_MAX_DEPTH,
    _is_subagent,
    _subagent_status,
    stop_subagent_tree,
)
from ..tools.ask_tools import reset_interactive as _reset_interactive_turn
from ..tools.ask_tools import set_interactive as _set_interactive_turn
from .config import _agent_lookup
from .messages_ui import _messages_to_ui

router = APIRouter()


class CreateSessionRequest(BaseModel):
    project_slug: str
    workspace: str
    agent_id: str | None = None
    title: str | None = None
    icon: str | None = None
    # Session kind (docs/floating-window-design.md §1.1): "quick" marks sessions
    # created by the floating quick-chat window; None/absent = regular session.
    type: str | None = None
    provider: str | None = None
    model: str | None = None
    # Context folder mounts (context-folders-design.md): library ids; the
    # session starts with these attached. Unknown ids are dropped silently.
    context_folders: list[str] = []
    primary_folder: str | None = None
    # Bound workflow for workflow-dev refine sessions (docs/workflow-dsl-design.md
    # §8.3). Injected into every turn's [turn context] as the current DSL.
    workflow_id: str | None = None
    # Subagent parentage (subagent-design.md §4). Set by the runtime scheduler
    # (subagent_scheduler.create_subagent), never by ordinary clients: the child
    # inherits the parent's slug-context and its depth caps the toolset.
    parent_session_id: str | None = None
    subagent: dict | None = None
    # Subagent type-registry tightening (P3 contract 1): fnmatch patterns the
    # matched type imposes ON TOP of the parent persona's tools_allow (the
    # persona filter keeps running at request time; this one filters the bound
    # toolset at graph-build time). Set only by the scheduler's spawn path.
    restrict_tools: list[str] = []
    # legacy aliases
    model_provider: str | None = None
    model_name: str | None = None


def _resolve_provider_model(req: CreateSessionRequest) -> tuple[str, str, str | None]:
    agent = _agent_lookup(req.agent_id)
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    # Prefer an *enabled* provider, in this order:
    #   1. explicit request (newSession opts / pin app)
    #   2. the agent's DELIBERATE provider choice — i.e. one that DIFFERS from
    #      the global default (设置 → 通用 → 默认模型提供商). Seed agents carry
    #      provider="custom", which equals the seeded default; letting that
    #      placeholder outrank the user's global choice made the General
    #      Settings selector dead (2026-10-02 fix). An agent binding that
    #      matches the default is the same outcome either way.
    #   3. the enabled global default ("enable a provider and use it" without
    #      editing every agent still works).
    default_pid = prov_mod.get_default_provider()
    from ..agents.registry import provider_is_deliberate

    candidates = [
        req.provider,
        req.model_provider,
        (agent.provider if provider_is_deliberate(agent) else None),
        default_pid,
    ]
    provider = next((c for c in candidates if _enabled(c)), None) or prov_mod.get_default_provider()
    model = (
        req.model
        or req.model_name
        or (agent.model if agent and agent.model else None)
        or prov_mod.model_for_provider(providers, provider)
    )
    return provider, model, (agent.id if agent else req.agent_id)


def _build_model_with_fallback(provider: str, model_name: str | None):
    """Build the session model, falling back to the global default config.

    Decision Q3 runtime side: an agent may still carry a binding that no
    longer resolves (config deleted/disabled, or its model left models[]).
    That must degrade, not crash the session: log a warning and rebuild from
    the default config + its default model. Re-raises only when the default
    itself is broken (nothing to fall back to).
    """
    try:
        return build_model(provider, model_name)
    except ValueError as e:
        dflt = prov_mod.get_default_provider()
        dflt_model = prov_mod.model_for_provider(prov_mod.load_providers(), dflt)
        if dflt == provider and (model_name or None) == (dflt_model or None):
            raise
        logging.getLogger(__name__).warning(
            "model build failed for provider=%s model=%s (%s); "
            "falling back to default config %s/%s",
            provider,
            model_name,
            e,
            dflt,
            dflt_model,
        )
        return build_model(dflt, dflt_model)


def _default_title(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return f"{a.name} session" if a else "New Session"


def _agent_icon(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return a.icon if a else "message-square"


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
    from . import stream as _stream_api

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


# ---- sessions CRUD ----


@router.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    provider, model_name, agent_id = _resolve_provider_model(req)
    try:
        model = _build_model_with_fallback(provider, model_name)
    except ValueError as e:
        return {"error": str(e), "ok": False}

    mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
    session_id = uuid.uuid4().hex
    # Subagent depth (subagent-design.md §4.2): one above the parent's; the
    # depth caps the toolset STRUCTURALLY (a depth-2 child never gets
    # spawn_subagent) and is rejected outright beyond it.
    sub_depth: int | None = None
    if req.parent_session_id:
        parent_meta, _ = _find_meta(req.parent_session_id) or ({}, None)
        sub_depth = subagent_depth_of(parent_meta) + 1
        if sub_depth > SUBAGENT_MAX_DEPTH:
            return {
                "ok": False,
                "error": (
                    f"subagent 嵌套深度已达上限（{SUBAGENT_MAX_DEPTH + 1} 层）"
                ),
            }
    # Every session gets its own files directory, created now and PRESERVED on
    # delete. It supersedes the client-supplied `workspace` (a shared, non-
    # session-scoped path) as the authoritative home for this session's files.
    session_dir = paths.session_files_dir(req.project_slug, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(session_dir)
    # Context folder mounts: filter to known library ids; a primary must be
    # one of the mounts (context-folders-design.md §4.1).
    folder_ids = [fid for fid in (req.context_folders or []) if cf.get_folder(fid)]
    primary_id = req.primary_folder if req.primary_folder in folder_ids else None
    context_dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    for d in context_dirs:
        if not d.get("missing"):
            cf.touch_folder(d["id"])
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=req.project_slug,
        session_id=session_id,
        context_dirs=context_dirs,
        primary_path=primary_path,
        subagent_depth=sub_depth,
        restrict_tools=req.restrict_tools,
    )
    graph = build_graph(
        model=model,
        project_slug=req.project_slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
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
        "context_folders": folder_ids,
        "primary_folder": primary_id,
        "workflow_id": req.workflow_id,
        "created": time.time(),
        "updated": time.time(),
    }
    if req.type:
        meta["type"] = req.type
    if sub_depth is not None:
        # Subagent meta (contract 1): the parent link + the subagent payload
        # are part of the session meta the list API and the frontend read.
        meta["type"] = "subagent"
        meta["parent_session_id"] = req.parent_session_id
        meta["depth"] = sub_depth
        meta["subagent"] = req.subagent or {
            "goal": "",
            "constraints": "",
            "acceptance": "",
            "origin": "agent",
            "status": "running",
            "result_summary": "",
        }
    _session_meta_upsert(req.project_slug, meta)
    _log.info(
        "session_create session=%s agent=%s provider=%s model=%s title=%r folders=%d",
        session_id,
        agent_id,
        provider,
        model_name,
        title,
        len(folder_ids),
    )
    s_entry = {
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
        # Context folder mounts (context-folders-design.md)
        "context_dirs": context_dirs,
        "primary_folder": primary_id,
        "primary_path": primary_path or "",
        "workflow_id": req.workflow_id,
        "type": req.type,
        # Type-registry tightening survives graph rebuilds via this entry
        # (turn.py's WorldState roster re-reads it too).
        "restrict_tools": [p for p in (req.restrict_tools or []) if isinstance(p, str) and p.strip()],
    }
    if sub_depth is not None:
        s_entry["parent_session_id"] = req.parent_session_id
        s_entry["depth"] = sub_depth
        # reverse index for the scheduler's tree walks (server_shared)
        shared.subagent_link_child(req.parent_session_id or "", session_id)
    _SESSIONS[session_id] = s_entry
    # return the meta shape (with `id`) so the frontend SessionMeta matches
    return {**meta, "ok": True}


@router.get("/api/sessions")
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


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict | None:
    m = _resolve_session_meta(session_id)
    if m is None:
        return None
    return {k: v for k, v in m.items() if k != "graph"}


class PatchSessionRequest(BaseModel):
    title: str | None = None
    icon: str | None = None
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    workflow_id: str | None = None


@router.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: PatchSessionRequest) -> dict:
    s = _SESSIONS.get(session_id)
    slug = s["project_slug"] if s else "default"
    patch = req.model_dump()

    # Per-session model switch (composer model chip): validate before touching
    # meta, then drop the in-memory graph so the next WS connect rebuilds it
    # via _ensure_session with the new model — same heal path as
    # PUT /api/providers. A goal driver holding the old session object keeps
    # the old model until that reconnect; accepted.
    switched = patch.get("provider") is not None or patch.get("model") is not None
    if switched:
        cur_meta = next(
            (m for m in _session_meta_list(slug) if m.get("id") == session_id), None
        ) or {}
        provider = (
            patch.get("provider") or (s or {}).get("provider") or cur_meta.get("provider")
        )
        if patch.get("provider") is not None and patch.get("model") is None:
            # provider change without an explicit model → that provider's
            # configured default, so the meta never keeps a foreign model.
            patch["model"] = prov_mod.model_for_provider(
                prov_mod.load_providers(), patch["provider"]
            )
        try:
            build_model(provider, patch.get("model"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    # Inspect the stored meta to honour the title_auto flag. Auto titles come
    # from the first user message (stream._touch_session_title); an explicit
    # rename sticks and an agent switch no longer renames.
    cur = next((m for m in _session_meta_list(slug) if m.get("id") == session_id), None)
    title_auto = (cur or {}).get("title_auto", True)
    if patch.get("title") is not None:
        title_auto = False
        # manual rename wins over a pending LLM subject title (title_gen)
        patch["title_llm_pending"] = False
    patch["title_auto"] = title_auto

    updated = _session_meta_patch(slug, session_id, patch)
    if switched:
        _SESSIONS.pop(session_id, None)
        s = None
    if s:
        for k, v in patch.items():
            if v is not None:
                s[k] = v
        s["title_auto"] = title_auto
    return {
        "ok": True,
        "session": updated or (s and {k: v for k, v in s.items() if k != "graph"}),
    }


@router.get("/api/external-agents")
async def list_external_agents() -> list[dict]:
    """Backend detection for Settings → External Agents（安装状态 + 路径）。"""
    from ..tools.external_agent import _BACKENDS

    out: list[dict] = []
    for _name, _be in _BACKENDS.items():
        try:
            _path = _be.available()
        except Exception:  # noqa: BLE001 — 检测绝不 500
            _path = None
        # 已安装但未完成首次初始化(pi 需要终端跑一次)→ 前端给引导提示
        _hint = ""
        if _path:
            try:
                _hint = "" if _be.configured() else "needsInit"
            except Exception:  # noqa: BLE001
                _hint = ""
        out.append({"name": _name, "installed": bool(_path), "path": _path or "",
                    "setupHint": _hint})
    return out


@router.post("/api/delegations/{delegation_id}/stop")
async def stop_delegation(delegation_id: str) -> dict:
    """取消运行中的外部委托：直杀子进程，bg 随后自行回填 error 终态并注入。"""
    from ..tools.external_agent import (
        _ACTIVE_DELEGATIONS,
        _ACTIVE_LOCK,
        _kill_tree,
    )

    with _ACTIVE_LOCK:
        entry = _ACTIVE_DELEGATIONS.get(delegation_id)
    if not entry:
        return {"ok": True, "killed": False, "reason": "not running"}
    _kill_tree(entry["proc"])
    return {"ok": True, "killed": True}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, cascade: bool = False) -> dict:
    """Delete a session: its index entry, on-disk checkpoint history, and any
    in-memory graph cache. Returns ok=True even if the id was already gone.

    ``cascade`` (=1, sent by the web shell for a parent with descendants,
    subagent-design.md §5.8) is DECLARED for contract visibility: subagent
    descendants are always cascade-deleted/stopped regardless, so the flag
    only documents the client's intent (and the confirm-dialog count).

    The session's files directory (`sessions/<session_id>/`) is intentionally
    PRESERVED — only the conversation (checkpoint + index row) is removed. The
    files stay browsable/cleanable via Settings → 会话文件. Note the checkpoint
    is the *file* `sessions/<session_id>.json`; the preserved dir is
    `sessions/<session_id>/` — never glob `<session_id>*` here.
    """
    s = _SESSIONS.pop(session_id, None)
    slug = s["project_slug"] if s else None
    # Settle a live subagent BEFORE its meta disappears (major-5): once the
    # index row is gone the completion gate's _find_meta finds nothing and no
    # one would re-evaluate a WAITING ancestor left behind by the removal.
    try:
        from ..subagent_scheduler import finalize_for_delete

        await finalize_for_delete(session_id)
    except Exception:
        _log.exception("subagent_delete_finalize_failed session=%s", session_id)
    removed = False
    # find the slug from the on-disk index if not known from memory
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        cand = slug_dir.parent.parent.name
        if _session_meta_remove(cand, session_id):
            removed = True
            slug = slug or cand
    # Subagent cascade delete (subagent-design.md §5.8): cooperative-stop the
    # session + all descendants, WAIT for the turn tasks to unwind, then remove
    # each descendant's meta + checkpoint — never the reverse, or a live turn
    # would write its checkpoint back after the unlink.
    if slug:
        try:
            from ..subagent_scheduler import delete_subagent_tree

            cascade = await delete_subagent_tree(slug, session_id)
            if cascade:
                removed = True
        except Exception:
            _log.exception("subagent_cascade_delete_failed session=%s", session_id)
        shared.subagent_unlink_any(session_id)
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


# ---- session stop (P2 contract 4) ------------------------------------------


@router.post("/api/sessions/{session_id}/stop", status_code=202)
async def stop_session(session_id: str) -> dict:
    """Stop whatever the session is doing; ALWAYS 202 ``{"stopped": true}``.

    Four shapes (P2 shared contract 4):

    * running turn → the cooperative-stop semantics of the WS ``stop`` branch:
      pause an active goal first, set the session's ``_TURN_STOP`` event (the
      turn's own machinery unwinds + settles it), and for a subagent session
      cascade-stop the descendants + settle a waiting meta;
    * parked at a permission/ask_user interrupt → heal + broadcast via the
      same ``_stop_parked_turn`` path the WS branch uses;
    * waiting subagent (no live turn of its own) → ``stop_subagent_tree``:
      finalize it stopped and cascade over every descendant;
    * idle → a no-op that still answers 202 (idempotent UX).
    """
    from .stream import _stop_parked_turn  # lazy: api.stream imports this module

    s = _SESSIONS.get(session_id)
    slug = s["project_slug"] if s else _session_slug(session_id)
    mode = "idle"

    if session_id in _PENDING_RESUME:
        # Parked at an interrupt: no live task — heal + broadcast directly
        # (check-and-discard, so a racing second stop no-ops).
        mode = "parked"
        _PENDING_RESUME.discard(session_id)
        _PENDING_KIND.pop(session_id, None)
        tid = _RUNNING_TURNS.pop(session_id, "")
        sess = s or _ensure_session(session_id)
        if sess is not None:
            spawn_bg(_stop_parked_turn(sess, session_id, tid))
        # Subagent cascade (§5.6 配套规则): the WS stop branch runs it for a
        # parked subagent too — the heal above settles the session itself
        # "stopped" (via _stop_parked_turn → on_turn_stopped), and a stopped
        # subagent must take its running descendants with it. Without this the
        # HTTP path left them alive under a stopped parent: their results are
        # later refused at the terminal-parent gate and silently lost.
        stype = (s or {}).get("type")
        if stype is None:
            _pf = _find_meta(session_id)
            stype = (_pf[0] or {}).get("type") if _pf else None
        if stype == "subagent":
            from ..subagent_scheduler import on_stop_if_waiting, stop_descendants

            async def _stop_cascade(_sid: str = session_id) -> None:
                try:
                    await stop_descendants(_sid)
                except Exception:
                    _log.exception("stop_session_cascade_failed session=%s", _sid)

            spawn_bg(_stop_cascade())
            spawn_bg(on_stop_if_waiting(session_id))
    elif session_id in _RUNNING_TURNS or (
        (t := _TURN_TASKS.get(session_id)) is not None and not t.done()
    ):
        mode = "running"
        # Goal parity with the WS stop branch: an active goal would
        # auto-continue ~3s after the turn unwinds — pause it first.
        if slug:
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
                _log.exception("stop_session_goal_pause_failed session=%s", session_id)
        evt = _TURN_STOP.get(session_id)
        if evt is None:
            evt = _TURN_STOP[session_id] = asyncio.Event()
        evt.set()
        # Subagent cascade (§5.6 配套规则): a stopped running/waiting subagent
        # takes its descendants with it; a waiting meta with no live turn is
        # settled here. Main conversations keep their subagents running.
        stype = (s or {}).get("type")
        if stype is None:
            found = _find_meta(session_id)
            stype = (found[0] or {}).get("type") if found else None
        if stype == "subagent":
            from ..subagent_scheduler import on_stop_if_waiting, stop_descendants

            async def _stop_cascade(_sid: str = session_id) -> None:
                try:
                    await stop_descendants(_sid)
                except Exception:
                    _log.exception("stop_session_cascade_failed session=%s", _sid)

            spawn_bg(_stop_cascade())
            spawn_bg(on_stop_if_waiting(session_id))
    else:
        found = _find_meta(session_id)
        if found and _is_subagent(found[0]) and _subagent_status(found[0]) == "waiting":
            # A waiting subagent holds no turn: finalize it stopped and
            # cascade over the still-running descendants.
            mode = "waiting"
            try:
                await stop_subagent_tree(session_id, include_self=True)
            except Exception:
                _log.exception("stop_session_waiting_failed session=%s", session_id)
        # else: idle — no-op, still 202 (never leave a set event behind).

    _log.info("session_stop session=%s mode=%s", session_id, mode)
    return {"stopped": True, "mode": mode}


# ---- session context folders (context-folders-design.md §4.3) ---------------


class PutSessionContextRequest(BaseModel):
    folder_ids: list[str] = []
    primary_id: str | None = None


def _apply_context_to_live_session(
    s: dict, dirs: list[dict], primary_id: str | None, primary_path: str | None
) -> None:
    """Rebind a live session's tools/graph to a new mount set."""
    slug = s.get("project_slug") or "default"
    workspace = str(s.get("workspace") or "")
    s["context_dirs"] = dirs
    s["primary_folder"] = primary_id
    s["primary_path"] = primary_path or ""
    mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=slug,
        session_id=s.get("session_id", ""),
        context_dirs=dirs,
        primary_path=primary_path,
        # Keep the structural spawn cap across rebuilds (subagent-design.md §4.2).
        subagent_depth=s.get("depth") if s.get("type") == "subagent" else None,
        # And the type-registry tightening (P3 contract 1).
        restrict_tools=s.get("restrict_tools") or [],
    )
    s["graph"] = build_graph(
        model=s["model"],
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
        all_tools=all_tools,
    )
    s["all_tool_names"] = [t.name for t in all_tools]
    s["mcp_tool_names"] = [t.name for t in mcp_tools]


def apply_session_context(
    session_id: str, folder_ids: list[str], primary_id: str | None
) -> dict:
    """Persist a session's mount set and rebuild its live graph (shared by
    ``PUT /api/sessions/{id}/context`` and the ``/mount`` builtin command)."""
    s = _SESSIONS.get(session_id)
    found = None if s else _find_meta(session_id)
    if s is None and found is None:
        return {"ok": False, "error": "unknown session"}
    slug = s["project_slug"] if s else found[1]

    folder_ids = [fid for fid in (folder_ids or []) if isinstance(fid, str) and fid]
    unknown = [fid for fid in folder_ids if cf.get_folder(fid) is None]
    if unknown:
        return {"ok": False, "error": f"Unknown folder id: {unknown}"}
    if primary_id and primary_id not in folder_ids:
        primary_id = None  # primary must be one of the mounts

    # "" clears a stale primary (_session_meta_patch drops None values).
    _session_meta_patch(
        slug,
        session_id,
        {"context_folders": folder_ids, "primary_folder": primary_id or ""},
    )
    dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    for d in dirs:
        if not d.get("missing"):
            cf.touch_folder(d["id"])

    if s is not None:
        _apply_context_to_live_session(s, dirs, primary_id, primary_path)

    # Let connected clients refresh their mount chip without a full refetch.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _push_session_event(
                session_id,
                "session.context",
                {"context_folders": folder_ids, "primary_folder": primary_id},
            )
        )
    except RuntimeError:
        pass  # sync caller without a loop; clients reconcile on refetch

    _log.info(
        "session_context session=%s folders=%s primary=%s",
        session_id,
        folder_ids,
        primary_id,
    )
    return {
        "ok": True,
        "session": {
            "id": session_id,
            "context_folders": folder_ids,
            "primary_folder": primary_id,
        },
        "context_dirs": dirs,
        "primary_path": primary_path,
    }


@router.put("/api/sessions/{session_id}/context")
async def put_session_context(session_id: str, req: PutSessionContextRequest) -> dict:
    """Replace the session's mount set (idempotent full replacement)."""
    return apply_session_context(session_id, req.folder_ids, req.primary_id)


# ---- session history (rebuild the chat UI's block layout from checkpoints) ----


@router.get("/api/sessions/{session_id}/history")
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
        "messages": _messages_to_ui(
            messages, agent_id, attached, project_slug=slug, session_id=session_id
        ),
        "last_error": last_error or None,
    }


# ---- session bootstrap ----


def _first_agent_id() -> str | None:
    lst = agents_reg.list_agents()
    return lst[0].id if lst else None


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
        model = _build_model_with_fallback(provider, model_name)
    except ValueError:
        return None
    # Derive the workspace from paths (not meta) so legacy sessions whose meta
    # still holds the shared "/tmp/gw" converge on the per-session dir without
    # rewriting the index.
    workspace = str(paths.session_files_dir(slug, session_id))
    # Restore the persisted mount set (context-folders-design.md): sessions
    # resume with exactly the folders they had before the restart.
    folder_ids = meta.get("context_folders") or []
    primary_id = meta.get("primary_folder") or None
    context_dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
    # Restore the subagent depth so the structural spawn cap survives a runtime
    # restart (subagent-design.md §4.2) — the meta is the source of truth.
    sub_depth = meta.get("depth") if meta.get("type") == "subagent" else None
    # Re-derive the type-registry tightening (P3 contract 1) from the meta's
    # recorded agent_type: a type edited since spawn re-tightens; a type
    # DELETED since spawn loosens back to the parent persona (documented).
    restrict_tools: list[str] = []
    if sub_depth is not None:
        from ..subagent_types import restrict_for_meta

        restrict_tools = restrict_for_meta(meta)
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=slug,
        session_id=session_id,
        context_dirs=context_dirs,
        primary_path=primary_path,
        subagent_depth=sub_depth,
        restrict_tools=restrict_tools,
    )
    graph = build_graph(
        model=model,
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
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
        "context_dirs": context_dirs,
        "primary_folder": primary_id,
        "primary_path": primary_path or "",
        "workflow_id": meta.get("workflow_id"),
        "restrict_tools": restrict_tools,
    }
    if sub_depth is not None:
        s["type"] = "subagent"
        s["parent_session_id"] = meta.get("parent_session_id")
        s["depth"] = sub_depth
        shared.subagent_link_child(meta.get("parent_session_id") or "", session_id)
    _SESSIONS[session_id] = s
    return s
