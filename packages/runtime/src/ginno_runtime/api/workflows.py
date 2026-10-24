"""Workflow endpoints: definition CRUD + versioning, DSL synthesis from a
session trace, and the background run engine (create/cancel/resume/decide/
retry/delete/cleanup + orphan reconciliation).

The synthesis loop lives in ``workflows_synthesis.py`` and the run-engine
drivers in ``workflows_runs.py`` (both imported at the BOTTOM of this module —
they must not be imported directly). This module keeps the whole REST surface:
every helper name remains an attribute here, so server.py / todos.py /
stream.engine / scheduler.py imports and the tests' monkeypatching of
``ginno_runtime.api.workflows.*`` (build_model, build_all_tools, _SYNTH_TASKS)
keep working unchanged."""

from __future__ import annotations

import asyncio  # noqa: F401  (kept: module-surface parity; _base consumers)
import shutil
import time
from pathlib import Path  # noqa: F401  (kept: module-surface parity)

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ginno_runtime import jsonx as json

from .. import agents as agents_reg  # noqa: F401  (kept: _base consumers)
from .. import paths  # noqa: F401  (kept: _base consumers)
from .. import providers as prov_mod
from .. import server_shared as shared  # noqa: F401  (kept: _base consumers)
from .. import workflows as wf_store
from ..checkpointer import FileCheckpointer
from ..graph import (
    build_all_tools,  # noqa: F401  (kept: monkeypatch surface)
    text_of_content,
)
from ..models import build_model
from ..server_shared import (
    _RUN_WS,
    _WF_RUN_TASKS,
    _ev,
    _log,  # noqa: F401  (kept: _base consumers + endpoint error logs)
    _push_global_event,
    _push_run_event,
    _push_session_event,
)
from ..session_meta import _session_slug
from ..todos import sync_ledger
from ..workflows import dsl as wf_dsl  # noqa: F401  (kept: _base consumers)
from ..workflows import events as wf_events
from ..workflows import store as wf_storemod

router = APIRouter()


# ---- workflows ----


@router.get("/api/workflows")
async def list_workflows_endpoint() -> list[dict]:
    return wf_store.list_defs()


def _backfill_synthesis_adopted(syn_id: str | None, wf: dict | None) -> None:
    """Quality-plan §3.1: when a workflow was created (or updated, 方案B 阶段4)
    from a synthesis draft, backfill the case outcome (adopted + workflow id)
    so the funnel sees L2 success. Best-effort, never blocks the request."""
    if not (wf and syn_id):
        return
    try:
        from ..workflows import synthesis as wf_synth

        wf_synth.backfill_outcome(
            wf_synth.find_case_by_synthesis_id(syn_id),
            created=True,
            workflow_id=wf.get("id"),
            created_at=time.time(),
        )
    except Exception:
        pass


@router.post("/api/workflows")
async def create_workflow_endpoint(data: dict) -> dict:
    data = data or {}
    # 方案B 阶段4「从会话导入」: with ``workflow_id`` the draft DSL is applied
    # to an EXISTING recipe as v(N+1) (PUT semantics via update_def) instead of
    # creating a new one. Same synthesis-adoption backfill as the create path.
    target_id = data.get("workflow_id")
    if target_id:
        if not wf_store.get_def(target_id):
            raise HTTPException(status_code=404, detail="workflow not found")
        if not isinstance(data.get("dsl"), dict):
            raise HTTPException(status_code=400, detail="dsl object required")
        try:
            wf = wf_store.update_def(target_id, {"dsl": data["dsl"]})
        except ValueError as exc:
            # invalid DSL (store validates on write) — a 400 with the first
            # validation error, never an uncaught 500.
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not wf:
            raise HTTPException(status_code=404, detail="workflow not found")
        _backfill_synthesis_adopted(data.get("synthesis_id"), wf)
        await _push_global_event("workflows.changed", {})
        return {"ok": True, "workflow": wf}
    try:
        wf = wf_store.create_def(data)
    except ValueError as exc:
        # invalid DSL (store validates on write) — a 400 with the first
        # validation error, never an uncaught 500.
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # Quality-plan §3.1: if this workflow came from a synthesis draft, backfill
    # the case outcome (adopted + workflow id) so the funnel sees L2 success.
    _backfill_synthesis_adopted(data.get("synthesis_id"), wf)
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "workflow": wf}


@router.put("/api/workflows/{wf_id}")
async def update_workflow_endpoint(wf_id: str, data: dict) -> dict:
    try:
        wf = wf_store.update_def(wf_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if wf:
        await _push_global_event("workflows.changed", {})
    return {"ok": bool(wf), "workflow": wf}


@router.delete("/api/workflows/{wf_id}")
async def delete_workflow_endpoint(wf_id: str) -> dict:
    if wf_storemod.is_system_def(wf_id):
        return {"ok": False, "error": "Built-in workflow; cannot be deleted"}
    ok = wf_store.delete_def(wf_id)
    if ok:
        # The workflow's cross-run result cache indexes its steps' outputs;
        # with the definition gone the entries are unreachable (and stale).
        from ..workflows.result_cache import RunResultCache

        RunResultCache.drop(wf_id)
        await _push_global_event("workflows.changed", {})
    return {"ok": ok}


@router.get("/api/workflow_runs")
async def list_workflow_runs_endpoint(
    workflow_id: str | None = Query(None),
    status: str | None = Query(None),
    supervisor_pending: bool | None = Query(None),
) -> list[dict]:
    """List runs, newest first. Optional filters: ``workflow_id`` (exact),
    ``status`` (single value or comma-separated list, e.g. "running,paused"),
    and ``supervisor_pending=true`` (paused runs awaiting a human decision —
    the Studio decision inbox)."""
    return wf_store.list_runs(
        workflow_id=workflow_id, status=status, supervisor_pending=supervisor_pending
    )


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


@router.get("/api/workflows/{wf_id}/doctor")
async def doctor_workflow_endpoint(wf_id: str) -> dict:
    """Static dataflow lint + recent-run evidence (master-plan §4.2 / WF-02)."""
    wf = wf_store.get_def(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    from ..workflows import doctor as wf_doctor

    # Newest first; the doctor only needs a small window, scoped to this version.
    runs = wf_store.list_runs(wf_id)[:5]
    result = wf_doctor.run_doctor(
        wf.get("dsl") or {}, runs=runs, version=wf.get("version")
    )
    return {"ok": True, **result}


@router.post("/api/workflows/dry-run")
async def dry_run_workflow_endpoint(data: dict) -> dict:
    """Zero-LLM preflight for a DSL draft (stability plan P1d): normalize +
    validate + doctor + compile + entry-reachability. The SummarizeModal and
    the inspector use it to trial a draft before it is saved or run; the dev
    agent's ``workflow_dry_run`` tool shares the same core (workflows/dryrun.py),
    so the checks never drift. Node callables are built but NEVER executed, so
    no provider is needed."""
    dsl = (data or {}).get("dsl")
    if not isinstance(dsl, dict):
        raise HTTPException(status_code=400, detail="dsl object required")
    from ..workflows.dryrun import dry_run_dsl

    return dry_run_dsl(dsl)


@router.post("/api/workflows/summarize-from-session")
async def summarize_session_to_dsl(data: dict) -> dict:
    """Distill a session's conversation into a workflow DSL *draft* (not saved).
    The UI then creates a workflow from it (version 1) or opens the dev agent.

    Validation + case creation happen synchronously — the case directory exists
    when the response returns, so the 总结 panel shows the in-progress row
    immediately. The ≤3-attempt LLM loop then runs in the background and
    streams ``synthesis.event`` WS events; the response carries the
    synthesis_id and the UI awaits the terminal state via WS + polling
    (tests/ops use the ``_await`` endpoint)."""
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
    data = data or {}
    last_n = data.get("last_n")
    last_n = last_n if isinstance(last_n, int) and not isinstance(last_n, bool) and last_n > 0 else None
    # 方案B 阶段4: optional contiguous message-range selection (inclusively
    # indexed into the checkpoint message list). last_n keeps precedence.
    sel_msgs, msg_range = _resolve_trace_window(messages, data)
    trace = _trace_text(sel_msgs, last_n)
    provider = data.get("provider") or prov_mod.get_default_provider()
    try:
        model = build_model(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"model unavailable: {e}")

    # Quality-plan §3.1: record a replayable case (best-effort, never blocks).
    from ..workflows import synthesis as wf_synth

    tool_call_count = sum(len(getattr(m, "tool_calls", None) or []) for m in messages)
    session_stats = {"messages": len(messages), "tool_calls": tool_call_count}
    case_dir, synthesis_id = wf_synth.new_case(
        session_id,
        provider=provider,
        model=getattr(model, "model", None) or getattr(model, "model_name", "") or "",
        last_n=last_n,
        msg_range=msg_range,
        trace=trace,
        session_stats=session_stats,
        prompt_version=SYNTH_PROMPT_VERSION,
    )
    wf_synth.prune_cases()
    if case_dir is None:
        raise HTTPException(status_code=500, detail="failed to create synthesis case")

    # S4b: up to 3 self-correcting attempts (shared helper, also used by
    # replay). Each attempt is metered into the usage log as source=workflow.
    synth_model_name = getattr(model, "model", None) or getattr(model, "model_name", "") or ""
    _spawn_synth_task(
        synthesis_id,
        case_dir,
        _run_synthesis_bg(session_id, synthesis_id, case_dir, trace, model, usage_attr={
            "provider": provider,
            "model": synth_model_name,
            "session_id": session_id,
            "run_id": synthesis_id,
        }),
    )
    return {"ok": True, "synthesis_id": synthesis_id, "status": "started"}


@router.get("/api/workflows/summarize-trace/{session_id}")
async def summarize_trace_endpoint(session_id: str) -> dict:
    """Numbered trace rows for the Studio「从会话导入」picker (方案B 阶段4).

    One row per CHECKPOINT message — the EXACT indexing the ``range`` param of
    ``summarize-from-session`` slices — so the UI's start/end row selection
    maps 1:1. (``/api/sessions/{sid}/history`` merges consecutive assistant
    steps into one bubble and therefore cannot be indexed.) Text is truncated
    to 200 chars; this is a selection preview, not a reader."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    slug = _session_slug(session_id)
    if not slug:
        raise HTTPException(status_code=404, detail="session not found")
    tup = await FileCheckpointer(slug).aget_tuple({"configurable": {"thread_id": session_id}})
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") if tup and tup.checkpoint else []
    rows: list[dict] = []
    for i, m in enumerate(messages or []):
        if isinstance(m, HumanMessage):
            rows.append({"i": i, "role": "user", "text": text_of_content(m.content)[:200]})
        elif isinstance(m, AIMessage):
            rows.append({
                "i": i,
                "role": "assistant",
                "text": text_of_content(m.content)[:200],
                "tools": [tc.get("name") for tc in getattr(m, "tool_calls", None) or []],
            })
        elif isinstance(m, ToolMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            rows.append({
                "i": i,
                "role": "tool",
                "name": getattr(m, "name", "tool") or "tool",
                "text": c[:200],
            })
        else:
            rows.append({"i": i, "role": "system", "text": text_of_content(getattr(m, "content", ""))[:200]})
    return {"ok": True, "rows": rows, "count": len(messages or [])}


# ---- synthesis-case review API (quality-plan §3.2, UI in Settings) ----
@router.get("/api/synthesis/cases")
async def synthesis_cases_endpoint(limit: int = 100) -> dict:
    from ..workflows import synthesis as wf_synth

    cases = wf_synth.list_cases(limit=max(1, min(500, limit)))
    # Live "running" annotation: the in-process task table is the only source
    # of truth (empty after a restart → leftover cases render 未完成).
    for row in cases:
        t = _SYNTH_TASKS.get(row.get("synthesis_id"))
        row["running"] = bool(t is not None and not t.done())
    return {"ok": True, "cases": cases}


@router.get("/api/synthesis/cases/{synthesis_id}")
async def synthesis_case_detail_endpoint(synthesis_id: str) -> dict:
    from ..workflows import synthesis as wf_synth

    case = wf_synth.load_case(synthesis_id)
    if not case:
        raise HTTPException(status_code=404, detail="synthesis case not found")
    return {"ok": True, "case": case}


@router.post("/api/synthesis/cases/{synthesis_id}/_await")
async def await_synthesis_case_endpoint(synthesis_id: str) -> dict:
    """Test/ops helper: await the background synthesis task so callers can
    observe the terminal case state deterministically (mirrors the workflow
    run ``_await``). The UI uses WS + polling instead."""
    from ..workflows import synthesis as wf_synth

    if wf_synth.find_case_by_synthesis_id(synthesis_id) is None:
        raise HTTPException(status_code=404, detail="synthesis case not found")
    task = _SYNTH_TASKS.get(synthesis_id)
    err: str | None = None
    if task is not None:
        try:
            await task
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "case": wf_synth.load_case(synthesis_id), "error": err}


@router.get("/api/synthesis/stats")
async def synthesis_stats_endpoint(days: int = 30) -> dict:
    """Aggregate funnel counts + top failure labels (quality-plan §3.2)."""
    from ..workflows import synthesis as wf_synth

    cutoff = time.time() - max(1, days) * 86400
    cases = wf_synth.list_cases(limit=500)
    total = l1 = l2 = l3 = 0
    edit_distances: list[int] = []
    fail_labels: dict[str, int] = {}
    # stability plan P4: the funnel split by prompt_version so synth-4 vs
    # synth-5 (doctor feedback + catalog) compare directly in the UI.
    by_version: dict[str, dict] = {}
    for c in cases:
        if (c.get("ts") or 0) < cutoff:
            continue
        total += 1
        pv = c.get("prompt_version") or "unknown"
        g = by_version.setdefault(pv, {"total": 0, "l1_generated": 0, "l2_adopted": 0, "l3_first_run_done": 0})
        g["total"] += 1
        if c.get("status") == "ok":
            l1 += 1
            g["l1_generated"] += 1
        elif c.get("fail_stage"):
            fail_labels[c["fail_stage"]] = fail_labels.get(c["fail_stage"], 0) + 1
        oc = c.get("outcome") or {}
        if oc.get("created"):
            l2 += 1
            g["l2_adopted"] += 1
        fr = oc.get("first_run") or {}
        if fr.get("status") == "done":
            l3 += 1
            g["l3_first_run_done"] += 1
        elif fr.get("status") == "failed" and fr.get("failed_node"):
            label = f"exec.{fr['failed_node']}"
            fail_labels[label] = fail_labels.get(label, 0) + 1
        ed = oc.get("edit_distance")
        if isinstance(ed, int):
            edit_distances.append(ed)
    top = sorted(fail_labels.items(), key=lambda kv: -kv[1])[:8]
    return {
        "ok": True,
        "days": days,
        "total": total,
        "l1_generated": l1,
        "l2_adopted": l2,
        "l3_first_run_done": l3,
        "by_prompt_version": dict(sorted(by_version.items())),
        "avg_edit_distance": (
            round(sum(edit_distances) / len(edit_distances), 2) if edit_distances else None
        ),
        "top_fail_labels": [{"label": k, "count": v} for k, v in top],
    }


@router.post("/api/synthesis/replay/{synthesis_id}")
async def synthesis_replay_endpoint(synthesis_id: str, data: dict | None = None) -> dict:
    """Re-run synthesis on a stored case's trace (offline eval, quality-plan
    §3.4). Does not touch the original case; returns the fresh DSL."""
    from ..workflows import synthesis as wf_synth

    case = wf_synth.load_case(synthesis_id)
    if not case:
        raise HTTPException(status_code=404, detail="synthesis case not found")
    inp = case.get("input") or {}
    trace = inp.get("trace") or ""
    if not trace:
        raise HTTPException(status_code=400, detail="case has no stored trace")
    provider = (data or {}).get("provider") or inp.get("provider") or prov_mod.get_default_provider()
    try:
        model = build_model(provider)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"model unavailable: {e}")
    result = await _run_synthesis(trace, model, None, usage_attr={
        "provider": provider,
        "model": getattr(model, "model", None) or getattr(model, "model_name", "") or "",
        # offline replay: not attributable to a live session/run
        "session_id": None,
        "run_id": synthesis_id,
    })
    return {
        "ok": result["ok"],
        "dsl": result["dsl"],
        "errors": result["errors"],
        "fail_stage": result["fail_stage"],
        "attempts_used": result["attempts_used"],
        "prompt_version": SYNTH_PROMPT_VERSION,
        "synthesis_id": synthesis_id,
    }


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
    sup_override = data.get("supervisor_override")
    if sup_override is not None and not isinstance(sup_override, dict):
        raise HTTPException(status_code=400, detail="supervisor_override must be an object")
    # 预算参数(odw P1-③):max_agent_calls 覆盖 DSL 与全局默认(0=不限);
    # 负数是配置错误,400。
    max_calls = data.get("max_agent_calls")
    if max_calls is not None:
        try:
            max_calls = max(0, int(max_calls))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_agent_calls must be a non-negative integer")
    run = wf_store.create_run(
        wf, session_id=session_id, present_in_session_id=present_in, context_override=override,
        supervisor_override=sup_override,
        cache_disabled=bool(data.get("no_cache")),
        max_agent_calls=max_calls,
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
    ev = wf_events.append_event(run_id, "cancelled")
    _set_run_pending_interrupt(run_id, None)
    _set_run_status(run_id, "cancelled", error="cancelled by user")
    sync_ledger.set_status(run_id, "cancelled", "cancelled by user")
    # the cancelled event is a real run event like any engine event — push it
    # to the run channel too (studio e2e test_studio_run_ws: the channel must
    # be complete without a REST refetch).
    await _push_run_event(run_id, "run.event", {"run_id": run_id, "payload": ev})
    await _push_run_status(
        run.get("present_in_session_id"), run_id, {"run_id": run_id, "status": "cancelled"}
    )
    return {"ok": True, "status": "cancelled"}


@router.post("/api/workflow_runs/{run_id}/pause")
async def pause_workflow_run_endpoint(run_id: str) -> dict:
    """Request a manual pause of a RUNNING run (workflow-ux-redesign #14).

    Cooperative: the flag takes effect at the earliest safe boundary (a node
    entry or an agent step's tool-iteration boundary); the run then transitions
    to "paused" exactly like a human-node pause and resumes via /resume from
    the last committed superstep. If the run finishes before reaching a
    boundary it simply completes (the pending flag dies with the run)."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "running":
        raise HTTPException(status_code=409, detail=f"run not pausable (status={run.get('status')})")
    from ..workflows import engine as wf_engine

    if not wf_engine.request_pause(run_id):
        raise HTTPException(
            status_code=409,
            detail="run has no live execution loop yet (try again shortly)",
        )
    wf_events.append_event(run_id, "pause_requested")
    return {"ok": True, "status": "pausing"}


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


@router.post("/api/workflow_runs/{run_id}/present")
async def present_workflow_run_endpoint(run_id: str, data: dict) -> dict:
    """Bind a run to a presenting session (设计B「聊天打开本次运行」): subsequent
    run.* events stream into that session's sockets, so a chat can follow a run
    that started elsewhere (Studio trigger, headless). Re-presenting re-binds;
    an empty ``session_id`` on the run is backfilled with the presenter."""
    data = data or {}
    sid = data.get("session_id")
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if not run.get("session_id"):
        run["session_id"] = sid
    run["present_in_session_id"] = sid
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)
    await _push_session_event(
        sid,
        "run.bind",
        {"run_id": run_id, "workflow_id": run.get("workflow_id"), "present_in_session_id": sid},
    )
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "run": run}


@router.get("/api/workflow_runs/{run_id}/events")
async def workflow_run_events_endpoint(
    run_id: str, node_id: str | None = None, kind: str | None = None
) -> dict:
    return {"ok": True, "events": wf_events.read_events(run_id, node_id=node_id, kind=kind)}


@router.websocket("/api/ws/runs/{run_id}")
async def run_ws_endpoint(ws: WebSocket, run_id: str) -> None:
    """Run-scoped live channel (design B P2).

    The Studio observer subscribes by run_id — independent of which session
    the run is presented in, and the ONLY live channel for headless runs.
    Contract:
      * register FIRST (events emitted between snapshot and registration are
        never lost), then send one ``run.snapshot`` {run, events, last_seq}
        replayed from events.jsonl;
      * afterwards ``run.event`` (payload carries ``seq``) and ``run.status`` +
        ``run.snapshot`` frames are pushed by the drivers;
      * a reconnect gets a fresh snapshot — the client drops payloads whose
        seq is <= its last applied seq, so overlaps are harmless;
      * inbound traffic is ping only; run control stays on REST (one
        authoritative writer per control action)."""
    await ws.accept()
    run = wf_store.get_run(run_id)
    if not run:
        await ws.send_text(_ev("run.missing", {"run_id": run_id}))
        await ws.close()
        return
    _RUN_WS.setdefault(run_id, []).append(ws)
    try:
        events = wf_events.read_events(run_id)
        last_seq = events[-1].get("seq") if events else 0
        await ws.send_text(_ev("run.snapshot", {
            "run_id": run_id,
            "run": run,
            "events": events,
            "last_seq": last_seq,
        }))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                msg = {}
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await ws.send_text(_ev("run.pong", {"run_id": run_id}))
    except WebSocketDisconnect:
        pass
    finally:
        socks = _RUN_WS.get(run_id) or []
        _RUN_WS[run_id] = [w for w in socks if w is not ws]


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
        supervisor_override=run.get("supervisor_override"),
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


@router.post("/api/workflow_runs/{run_id}/retry_from_checkpoint")
async def retry_from_checkpoint_endpoint(run_id: str) -> dict:
    """Retry a FAILED run from its persisted checkpoint (workflow-ux-redesign
    P2): the new run re-executes only the failed node + the suffix, skipping
    the completed prefix. Falls back with a 409 when there is no checkpoint to
    continue from (the UI then offers the plain retry)."""
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") != "failed":
        raise HTTPException(
            status_code=409, detail=f"only failed runs retry from checkpoint (status={run.get('status')})"
        )
    if not (run.get("error_detail") or {}).get("node_id"):
        raise HTTPException(
            status_code=409, detail="failure has no node attribution — retry from the start"
        )
    ckpt = _run_checkpoint_path(run_id)
    if not ckpt.exists():
        raise HTTPException(
            status_code=409, detail="no checkpoint available — retry from the start"
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
        supervisor_override=run.get("supervisor_override"),
    )
    # The engine keys checkpoints by thread_id == run_id: clone the file so the
    # new run continues from the failed node instead of the entry. The record's
    # embedded session_id routes FileCheckpointer._write — it MUST be rewritten
    # to the new run id, otherwise resumed checkpoints land back in the source
    # run's file and the new run's copy stays frozen at the failure (the run
    # would then report "paused" forever).
    new_ckpt = _run_checkpoint_path(new["id"])
    shutil.copyfile(ckpt, new_ckpt)
    try:
        rec = json.loads(new_ckpt.read_text() or "{}")
        if isinstance(rec, dict):
            rec["session_id"] = new["id"]
            new_ckpt.write_text(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        _log.exception("checkpoint_clone_retag_failed run=%s new=%s", run_id, new["id"])
    run["retry_run_id"] = new["id"]
    run["updated"] = time.time()
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)
    sync_ledger.clone_for_retry(run_id, new["id"])
    _spawn_run_task(
        new["id"],
        _continue_run_bg(new["id"], run.get("workflow_id", ""), run.get("present_in_session_id")),
    )
    await _push_global_event("workflows.changed", {})
    return {"ok": True, "run": new, "source_run_id": run_id}


@router.post("/api/workflow_runs/{run_id}/rerun_from")
async def rerun_from_endpoint(run_id: str, data: dict) -> dict:
    """Fork a NEW run that re-executes from an arbitrary node of an existing
    run (design B 屏2). Unlike ``retry_from_checkpoint`` (failed runs, failed
    node only) this accepts any node that the source run ever scheduled and
    any non-running source status. The fork compiles the run's PINNED
    ``dsl_version`` — a version deleted from history is a 409, never a
    silent behavior change."""
    data = data or {}
    node_id = data.get("node_id")
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id required")
    run = wf_store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    if run.get("status") == "running":
        raise HTTPException(
            status_code=409, detail="cannot fork a running run — wait for it to finish or pause it"
        )
    if not _run_checkpoint_path(run_id).exists():
        raise HTTPException(status_code=409, detail="no checkpoint available — retry from the start")
    # def existence BEFORE version existence: delete_def removes the version
    # files too, so a deleted workflow must answer 404, not "version missing".
    wf = wf_store.get_def(run.get("workflow_id", ""))
    if not wf:
        raise HTTPException(status_code=404, detail="workflow definition no longer exists")
    version = run.get("dsl_version") or 0
    pinned = wf_storemod.get_version(run.get("workflow_id", ""), int(version)) if version else None
    if not pinned:
        raise HTTPException(
            status_code=409, detail=f"version v{version} of this workflow no longer exists"
        )
    if node_id not in {n.get("id") for n in pinned.get("nodes") or []}:
        raise HTTPException(status_code=409, detail=f"node '{node_id}' not in the run's DSL (v{version})")
    # A pinned pseudo-view: create_run pins dsl_version from `version`.
    new = wf_store.create_run(
        {"id": wf["id"], "name": wf.get("name"), "dsl": pinned, "version": int(version)},
        session_id=run.get("session_id"),
        present_in_session_id=run.get("present_in_session_id"),
        context_override=run.get("context_override"),
        retried_from=run_id,
        supervisor_override=run.get("supervisor_override"),
    )
    # Carry the source's completed prefix over so the fork doesn't read as
    # 0/N with a forever-pending prefix.
    src_status = {s.get("id"): s.get("status") for s in run.get("steps", [])}
    idx = next((i for i, s in enumerate(new["steps"]) if s["id"] == node_id), None)
    if idx:
        for s in new["steps"][:idx]:
            st = src_status.get(s["id"])
            if st in ("done", "skipped", "failed"):
                wf_store.update_step(new["id"], s["id"], st)
    ckpt, new_ckpt = _run_checkpoint_path(run_id), _run_checkpoint_path(new["id"])
    shutil.copyfile(ckpt, new_ckpt)
    try:
        rec = json.loads(new_ckpt.read_text() or "{}")
        if isinstance(rec, dict):
            rec["session_id"] = new["id"]
            new_ckpt.write_text(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:
        _log.exception("checkpoint_clone_retag_failed run=%s new=%s", run_id, new["id"])
    run["retry_run_id"] = new["id"]
    run["updated"] = time.time()
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)
    sync_ledger.clone_for_retry(run_id, new["id"])
    _spawn_run_task(
        new["id"],
        _rerun_from_bg(
            new["id"], run.get("workflow_id", ""), node_id, pinned, run.get("present_in_session_id")
        ),
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


# Re-export surface: the synthesis loop and the run-engine drivers were split
# into sibling modules (imported here at the BOTTOM so the api.workflows ↔
# api.todos module-level cycle keeps its current resolution order). Every name
# below stays an attribute of THIS module — server.py, todos.py,
# stream/engine.py and scheduler.py import them from here, and the tests
# monkeypatch ``ginno_runtime.api.workflows.build_model`` / ``build_all_tools``
# expecting the drivers to see the patch.
from .workflows_runs import (  # noqa: E402
    _apply_supervisor_override,
    _backfill_first_run,
    _continue_run_bg,
    _drive_run_events,
    _mark_run_failed,
    _push_both,
    _push_run_status,
    _reconcile_orphan_runs,
    _remove_run_artifacts,
    _rerun_from_bg,
    _resume_workflow_bg,
    _run_checkpoint_path,
    _run_workflow_bg,
    _set_run_pending_interrupt,
    _set_run_status,
    _shutdown_run_tasks,
    _spawn_run_task,
    _touch_run,
    _wf_build_deps,
    _wf_mcp_tools,
)
from .workflows_synthesis import (  # noqa: E402
    _SYNTH_TASKS,
    SYNTH_PROMPT_VERSION,
    _extract_json_obj,
    _resolve_trace_window,
    _run_synthesis,
    _run_synthesis_bg,
    _shutdown_synth_tasks,
    _spawn_synth_task,
    _synth_error_text,
    _synthesize_prompt,
    _trace_text,
)
