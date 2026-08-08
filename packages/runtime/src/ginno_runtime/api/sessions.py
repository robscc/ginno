"""Session endpoints: CRUD, goal management + the goal continuation driver,
session bootstrap (_ensure_session), and the persisted-history endpoint."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .. import agents as agents_reg
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
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _log,
    _push_session_event,
    _turn_lock,
)
from ..session_meta import (
    _find_meta,
    _resolve_session_meta,
    _session_meta_list,
    _session_meta_patch,
    _session_meta_remove,
    _session_meta_upsert,
    _session_slug,
)
from .config import _agent_lookup
from .messages_ui import _messages_to_ui

router = APIRouter()


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
    await _stream_api._run_stream(None, session["graph"], config, text, session, agent_id)


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
        model = build_model(provider, model_name)
    except ValueError as e:
        return {"error": str(e), "ok": False}

    mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
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


@router.patch("/api/sessions/{session_id}")
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


@router.delete("/api/sessions/{session_id}")
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
        "messages": _messages_to_ui(messages, agent_id, attached),
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
        model = build_model(provider, model_name)
    except ValueError:
        return None
    # Derive the workspace from paths (not meta) so legacy sessions whose meta
    # still holds the shared "/tmp/gw" converge on the per-session dir without
    # rewriting the index.
    workspace = str(paths.session_files_dir(slug, session_id))
    mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
    all_tools = build_all_tools(
        mcp_tools, workspace=workspace, project_slug=slug, session_id=session_id
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
    }
    _SESSIONS[session_id] = s
    return s
