"""Workflow endpoints: definition CRUD + versioning, DSL synthesis from a
session trace, and the background run engine (create/cancel/resume/decide/
retry/delete/cleanup + orphan reconciliation)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import agents as agents_reg
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from .. import workflows as wf_store
from ..checkpointer import FileCheckpointer
from ..graph import build_all_tools
from ..models import build_model
from ..server_shared import _WF_RUN_TASKS, _log, _push_global_event, _push_session_event
from ..session_meta import _session_slug
from ..todos import sync_ledger
from ..workflows import dsl as wf_dsl
from ..workflows import events as wf_events
from ..workflows import store as wf_storemod

router = APIRouter()


# ---- workflows ----


@router.get("/api/workflows")
async def list_workflows_endpoint() -> list[dict]:
    return wf_store.list_defs()


@router.post("/api/workflows")
async def create_workflow_endpoint(data: dict) -> dict:
    wf = wf_store.create_def(data)
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "workflow": wf}


@router.put("/api/workflows/{wf_id}")
async def update_workflow_endpoint(wf_id: str, data: dict) -> dict:
    wf = wf_store.update_def(wf_id, data)
    if wf:
        await _push_global_event("workflows.changed", {})
    return {"ok": bool(wf), "workflow": wf}


@router.delete("/api/workflows/{wf_id}")
async def delete_workflow_endpoint(wf_id: str) -> dict:
    if wf_storemod.is_system_def(wf_id):
        return {"ok": False, "error": "内置 workflow，不可删除"}
    ok = wf_store.delete_def(wf_id)
    if ok:
        await _push_global_event("workflows.changed", {})
    return {"ok": ok}


@router.get("/api/workflow_runs")
async def list_workflow_runs_endpoint() -> list[dict]:
    return wf_store.list_runs()


@router.get("/api/workflows/{wf_id}")
async def get_workflow_endpoint(wf_id: str) -> dict:
    """Full definition view: current DSL + version + legacy steps projection."""
    wf = wf_store.get_def(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"ok": True, "workflow": wf}


@router.get("/api/workflows/{wf_id}/versions")
async def list_workflow_versions_endpoint(wf_id: str) -> dict:
    return {"ok": True, "versions": wf_store.list_versions(wf_id)}


@router.get("/api/workflows/{wf_id}/versions/diff")
async def diff_workflow_versions_endpoint(wf_id: str, a: int, b: int) -> dict:
    diff = wf_store.diff_versions(wf_id, a, b)
    if diff is None:
        raise HTTPException(status_code=404, detail="version not found")
    return {"ok": True, "a": a, "b": b, "diff": diff}


@router.get("/api/workflows/{wf_id}/versions/{n}")
async def get_workflow_version_endpoint(wf_id: str, n: int) -> dict:
    v = wf_store.get_version(wf_id, n)
    if v is None:
        raise HTTPException(status_code=404, detail="version not found")
    return {"ok": True, "version": n, "dsl": v}


@router.post("/api/workflows/{wf_id}/rollback")
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


@router.post("/api/workflows/summarize-from-session")
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
        return shared._mcp.all_langchain_tools() if shared._mcp else []
    except Exception:
        return []


def _wf_build_deps(run_id: str, workflow_id: str):
    """Resolve (wf, dsl, model, tools, fork_id) for a run by forking its source
    agent. Returns a 5-tuple of None when the workflow def is missing.

    NOTE: fork_agent/build_model can RAISE (unknown agent, disabled/keyless
    provider) — callers must invoke this inside a try/except that marks the run
    failed, or the run is stranded in "running" forever.
    """
    wf = wf_store.get_def(workflow_id)
    if not wf:
        return None, None, None, None, None
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


def _set_run_status(
    run_id: str,
    status: str,
    error: str | None = None,
    only_from: tuple[str, ...] | None = None,
) -> None:
    """Persist a run status transition.

    ``error`` is stored when given and cleared on ``done``; terminal statuses
    stamp ``finished``. ``only_from`` guards races (e.g. a queued engine "done"
    must not overwrite a user cancel that landed first).
    """
    run = wf_store.get_run(run_id)
    if not run:
        return
    if only_from is not None and run.get("status") not in only_from:
        return
    run["status"] = status
    run["updated"] = time.time()
    if error is not None:
        run["error"] = error
    if status == "done":
        run["error"] = None
    if status in wf_storemod.TERMINAL_STATUSES and not run.get("finished"):
        run["finished"] = run["updated"]
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)


def _touch_run(run_id: str) -> None:
    """Bump ``updated`` so the UI's stuck-detection sees activity even while a
    single long step runs (called per engine event)."""
    run = wf_store.get_run(run_id)
    if run:
        run["updated"] = time.time()
        wf_storemod._write_json(wf_storemod._run_path(run_id), run)


async def _mark_run_failed(run_id: str, exc: BaseException, present_in: str | None = None) -> None:
    """The one place a run failure is persisted: error event + run.error +
    ledger + run.status push (so chat/panel show the reason immediately)."""
    err = f"{type(exc).__name__}: {exc}"
    wf_events.append_event(run_id, "error", error=err)
    _set_run_status(run_id, "failed", error=err)
    sync_ledger.set_status(run_id, "failed", err)
    await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "failed", "error": err})


async def _drive_run_events(run_id: str, present_in: str | None, wf: dict, agen) -> None:
    """Persist + push each engine event; keep run step status + terminal state in sync."""
    node_to_step = {s["id"]: s["id"] for s in wf.get("steps", [])}
    async for ev in agen:
        wf_events.append_event(run_id, ev.get("kind", ""), **{
            k: v for k, v in ev.items() if k not in ("kind", "run_id")
        })
        _touch_run(run_id)
        await _push_session_event(present_in, "run.event", {"run_id": run_id, "payload": ev})
        kind = ev.get("kind")
        nid = ev.get("node_id")
        if kind == "node_enter" and nid in node_to_step:
            wf_store.update_step(run_id, node_to_step[nid], "running")
        elif kind == "node_exit" and nid in node_to_step:
            wf_store.update_step(run_id, node_to_step[nid], "done" if ev.get("status") != "failed" else "failed")
        elif kind == "done":
            _set_run_status(run_id, "done", only_from=("running", "paused"))
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "done"})
        elif kind == "paused":
            _set_run_status(run_id, "paused", only_from=("running", "paused"))
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "paused"})
        elif kind == "error":
            # Mark the run failed FIRST: flipping a still-running step to
            # "failed" via update_step while the run were still "running" would
            # recompute it to "done" (all steps terminal) and the failed write
            # below would then be a no-op. With the run already "failed",
            # update_step only touches the step, not the run status.
            _set_run_status(run_id, "failed", error=str(ev.get("error") or ""), only_from=("running", "paused"))
            run = wf_store.get_run(run_id)
            if run:
                for s in run.get("steps", []):
                    if s.get("status") == "running":
                        wf_store.update_step(run_id, s["id"], "failed")
            await _push_session_event(
                present_in, "run.status",
                {"run_id": run_id, "status": "failed", "error": str(ev.get("error") or "")},
            )


async def _run_workflow_bg(run_id: str, workflow_id: str, context_override: dict | None, present_in: str | None = None) -> None:
    """Background driver: fork agent, stream the engine, persist + push events.

    Invariant: the run NEVER ends this coroutine still marked "running" — every
    failure (including dependency-building errors) lands as "failed" with the
    error persisted. CancelledError is re-raised: the cancel endpoint /
    shutdown path owns that status flip.
    """
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        wf, dsl, model, tools, fork_id = _wf_build_deps(run_id, workflow_id)
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        await _push_session_event(present_in, "run.bind", {"run_id": run_id, "workflow_id": workflow_id, "present_in_session_id": present_in})
        agen = wf_engine.run_workflow(dsl, run_id=run_id, model=model, tools=tools, context_override=context_override)
        await _drive_run_events(run_id, present_in, wf, agen)
        sync_ledger.set_status(run_id, "ok")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("workflow_run_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        # The fork is a per-run scratch agent; drop it so the Agents list
        # doesn't accumulate wf-* clutter (reruns/resumes re-fork idempotently).
        if fork_id:
            try:
                agents_reg.delete_agent(fork_id)
            except Exception:
                pass
        # Headless runs (todo sync et al.) have no present_in session; push
        # globally so the Workflow panel lists them without a manual refresh.
        await _push_global_event("workflows.changed", {})


async def _resume_workflow_bg(run_id: str, workflow_id: str, resume_value: dict, present_in: str | None = None) -> None:
    """Background driver to continue a paused run (human/supervisor decision).
    Same never-silent-running invariant as :func:`_run_workflow_bg`."""
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        wf, dsl, model, tools, fork_id = _wf_build_deps(run_id, workflow_id)
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        _set_run_status(run_id, "running")
        agen = wf_engine.resume_workflow(dsl, run_id=run_id, model=model, tools=tools, resume_value=resume_value)
        await _drive_run_events(run_id, present_in, wf, agen)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("workflow_resume_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        # paused-at-interrupt runs re-fork idempotently on next resume
        if fork_id:
            try:
                agents_reg.delete_agent(fork_id)
            except Exception:
                pass


def _spawn_run_task(run_id: str, coro) -> asyncio.Task:
    """Register a run's background task and attach the permanent safety net.

    The done-callback guarantees the "never silently running" invariant even if
    a future code path forgets a status write: a non-cancelled task that ends
    while the on-disk run is still "running" is healed to "failed". Also prunes
    ``_WF_RUN_TASKS`` (the ``_await`` endpoint falls back to a disk read).
    """
    task = asyncio.create_task(coro)
    _WF_RUN_TASKS[run_id] = task

    def _on_done(t: asyncio.Task) -> None:
        _WF_RUN_TASKS.pop(run_id, None)
        if t.cancelled():
            return
        try:
            run = wf_store.get_run(run_id)
            if run and run.get("status") == "running":
                exc = t.exception()
                err = (
                    f"{type(exc).__name__}: {exc}"
                    if exc is not None
                    else "run task ended without emitting a terminal event"
                )
                wf_events.append_event(run_id, "error", error=err)
                _set_run_status(run_id, "failed", error=err)
                sync_ledger.set_status(run_id, "failed", err)
                _log.error("workflow_run_guard_healed run=%s err=%s", run_id, err)
        except Exception:
            _log.exception("workflow_run_guard_failed run=%s", run_id)

    task.add_done_callback(_on_done)
    return task


def _reconcile_orphan_runs() -> None:
    """Heal runs stranded by a previous process exit (startup reconciliation).

    Called once in the lifespan startup: at that moment ``_WF_RUN_TASKS`` is
    empty and no run task can possibly be alive, so every run still marked
    "running" is by definition an orphan from a crash/quit. Each is flipped to
    the terminal ``interrupted`` status with a persisted reason + an event (which
    also (re)creates its events file). "paused" runs are left untouched — their
    checkpoint is intact and they stay resumable. Failed runs missing an inline
    ``error`` get it backfilled from their last error event so the UI can show
    the reason without parsing events.jsonl.
    """
    interrupted = 0
    backfilled = 0
    for run in wf_store.list_runs():
        rid = run.get("id")
        status = run.get("status")
        if status == "running":
            reason = "sidecar restarted during run (orphaned)"
            wf_events.append_event(rid, "interrupted", error=reason)
            _set_run_status(rid, "interrupted", error=reason)
            sync_ledger.set_status(rid, "interrupted", reason)
            interrupted += 1
        elif status == "failed" and not run.get("error"):
            errs = wf_events.read_events(rid, kind="error")
            if errs:
                last = errs[-1].get("error") or ""
                if last:
                    run["error"] = last
                    wf_storemod._write_json(wf_storemod._run_path(rid), run)
                    backfilled += 1
    if interrupted or backfilled:
        _log.info(
            "run_reconciliation interrupted=%d backfilled=%d", interrupted, backfilled
        )


async def _shutdown_run_tasks() -> None:
    """Graceful shutdown: cancel live run tasks, then mark any run still
    "running" on disk as interrupted. Runs already paused stay paused."""
    pending = [(rid, t) for rid, t in list(_WF_RUN_TASKS.items()) if not t.done()]
    for _, t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(t for _, t in pending), return_exceptions=True),
                timeout=3.0,
            )
        except (TimeoutError, asyncio.CancelledError):
            pass
    for rid, _ in pending:
        run = wf_store.get_run(rid)
        if run and run.get("status") == "running":
            reason = "sidecar shut down during run"
            wf_events.append_event(rid, "interrupted", error=reason)
            _set_run_status(rid, "interrupted", error=reason)
            sync_ledger.set_status(rid, "interrupted", reason)


@router.post("/api/workflow_runs")
async def create_workflow_run_endpoint(data: dict) -> dict:
    """Trigger a workflow run: creates the run (bound to a session for in-chat
    rendering), forks the agent, executes in the background. Returns immediately."""
    data = data or {}
    workflow_id = data.get("workflow_id")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id required")
    wf = wf_store.get_def(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    session_id = data.get("session_id")
    present_in = data.get("present_in_session_id") or session_id
    override = data.get("context_override")
    run = wf_store.create_run(
        wf, session_id=session_id, present_in_session_id=present_in, context_override=override
    )
    _spawn_run_task(run["id"], _run_workflow_bg(run["id"], workflow_id, override, present_in))
    return {"ok": True, "run": run}


@router.post("/api/workflow_runs/{run_id}/cancel")
async def cancel_workflow_run_endpoint(run_id: str) -> dict:
    """Cancel a running workflow run (stops the background task)."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    task = _WF_RUN_TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()
    wf_events.append_event(run_id, "cancelled")
    _set_run_status(run_id, "cancelled", error="cancelled by user")
    sync_ledger.set_status(run_id, "cancelled", "cancelled by user")
    await _push_session_event(run.get("present_in_session_id"), "run.status", {"run_id": run_id, "status": "cancelled"})
    return {"ok": True, "status": "cancelled"}


@router.post("/api/workflow_runs/{run_id}/resume")
async def resume_workflow_run_endpoint(run_id: str, data: dict) -> dict:
    """Resume a paused run with a value (e.g. {"decision":..., "context_patch":{...}})."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "paused":
        raise HTTPException(status_code=409, detail=f"run not paused (status={run.get('status')})")
    _spawn_run_task(run_id, _resume_workflow_bg(run_id, run["workflow_id"], data or {}, run.get("present_in_session_id")))
    return {"ok": True, "status": "resuming"}


@router.post("/api/workflow_runs/{run_id}/decide")
async def decide_workflow_run_endpoint(run_id: str, data: dict) -> dict:
    """Supervisor/human decision = resume with {"decision","context_patch"}."""
    data = data or {}
    value = {"decision": data.get("decision"), "context_patch": data.get("context_patch") or {}}
    return await resume_workflow_run_endpoint(run_id, value)


@router.get("/api/workflow_runs/{run_id}")
async def get_workflow_run_endpoint(run_id: str) -> dict:
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return {"ok": True, "run": run}


@router.get("/api/workflow_runs/{run_id}/events")
async def workflow_run_events_endpoint(
    run_id: str, node_id: str | None = None, kind: str | None = None
) -> dict:
    return {"ok": True, "events": wf_events.read_events(run_id, node_id=node_id, kind=kind)}


@router.post("/api/workflow_runs/{run_id}/_await")
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


def _run_checkpoint_path(run_id: str) -> Path:
    """Checkpoint artifact for a run. Engine compiles with
    ``FileCheckpointer("default")`` + ``thread_id=run_id`` (server never
    overrides the slug), so all run checkpoints land in the default project's
    sessions dir under the run id. Exact-name only — never glob (per-session
    dirs share prefixes; same rule as delete_session)."""
    return paths.project_sessions_dir("default") / f"{run_id}.json"


def _remove_run_artifacts(run_id: str) -> None:
    """Delete a run's persisted files: run JSON + events JSONL + checkpoint."""
    wf_store.delete_run(run_id)
    _run_checkpoint_path(run_id).unlink(missing_ok=True)


@router.post("/api/workflow_runs/{run_id}/retry")
async def retry_workflow_run_endpoint(run_id: str) -> dict:
    """Re-run a failed/interrupted/cancelled run as a NEW run.

    Carries over the original's context_override and session binding (an
    in-chat run re-appears in the same conversation; a headless run stays
    headless) and pins the CURRENT DSL version. Retry is always a deliberate,
    manual action — there is no automatic retry anywhere (todo-push safety).
    """
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") not in ("failed", "interrupted", "cancelled"):
        raise HTTPException(
            status_code=409, detail=f"run not retryable (status={run.get('status')})"
        )
    wf = wf_store.get_def(run.get("workflow_id", ""))
    if not wf:
        raise HTTPException(status_code=404, detail="workflow definition no longer exists")
    override = run.get("context_override")
    new = wf_store.create_run(
        wf,
        session_id=run.get("session_id"),
        present_in_session_id=run.get("present_in_session_id"),
        context_override=override,
        retried_from=run_id,
    )
    # Stamp the original so the UI can show "已重试" and hide the retry button.
    run["retry_run_id"] = new["id"]
    run["updated"] = time.time()
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)
    sync_ledger.clone_for_retry(run_id, new["id"])
    _spawn_run_task(
        new["id"],
        _run_workflow_bg(new["id"], run.get("workflow_id", ""), override, run.get("present_in_session_id")),
    )
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "run": new, "source_run_id": run_id}


@router.delete("/api/workflow_runs/{run_id}")
async def delete_workflow_run_endpoint(run_id: str) -> dict:
    """Delete a run and all its artifacts (JSON, events JSONL, checkpoint).

    Running/paused runs are cancelled first. Cancel → unlink with no await in
    between: on the single-threaded loop the cancelled coroutine cannot rewrite
    the JSON before the unlink (CancelledError bypasses the status-writing
    except), so the run cannot resurrect."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") in ("running", "paused"):
        task = _WF_RUN_TASKS.get(run_id)
        if task is not None and not task.done():
            task.cancel()
    _remove_run_artifacts(run_id)
    _WF_RUN_TASKS.pop(run_id, None)
    sync_ledger.set_status(run_id, "deleted")
    await _push_global_event("workflows.changed", {})
    return {"ok": True}


@router.post("/api/workflow_runs/cleanup")
async def cleanup_workflow_runs_endpoint(data: dict) -> dict:
    """Bulk-delete runs in terminal statuses (default: all terminal statuses).

    Terminal runs have no live task by construction (done-callback guard +
    startup reconciliation), so no cancellation is needed. Also sweeps orphan
    events files whose run JSON is already gone."""
    data = data or {}
    want = set(data.get("statuses") or wf_storemod.TERMINAL_STATUSES)
    want &= set(wf_storemod.TERMINAL_STATUSES)
    deleted = 0
    for run in wf_store.list_runs():
        if run.get("status") in want:
            _remove_run_artifacts(run.get("id", ""))
            _WF_RUN_TASKS.pop(run.get("id", ""), None)
            deleted += 1
    # Sweep orphan events files (run JSON gone but <id>.events.jsonl remains).
    runs_dir = wf_storemod._runs_dir()
    if runs_dir.exists():
        for p in runs_dir.glob("*.events.jsonl"):
            rid = p.name[: -len(".events.jsonl")]
            if not (runs_dir / f"{rid}.json").exists():
                p.unlink(missing_ok=True)
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "deleted": deleted}
