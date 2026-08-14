"""Workflow endpoints: definition CRUD + versioning, DSL synthesis from a
session trace, and the background run engine (create/cancel/resume/decide/
retry/delete/cleanup + orphan reconciliation)."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from .. import agents as agents_reg
from .. import paths
from .. import providers as prov_mod
from .. import server_shared as shared
from .. import workflows as wf_store
from ..checkpointer import FileCheckpointer
from ..graph import build_all_tools, text_of_content
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
    # Quality-plan §3.1: if this workflow came from a synthesis draft, backfill
    # the case outcome (adopted + workflow id) so the funnel sees L2 success.
    syn_id = (data or {}).get("synthesis_id")
    if wf and syn_id:
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


@router.get("/api/workflows/{wf_id}/doctor")
async def doctor_workflow_endpoint(wf_id: str) -> dict:
    """Static dataflow lint (master-plan §4.2): no LLM, millisecond-fast."""
    wf = wf_store.get_def(wf_id)
    if not wf:
        raise HTTPException(status_code=404, detail="workflow not found")
    from ..workflows import doctor as wf_doctor

    result = wf_doctor.run_doctor(wf.get("dsl") or {})
    return {"ok": True, **result}


# ---- P6: synthesize a workflow DSL draft from a session's conversation ----
# Bump on any change to the prompt above so synthesis cases can be grouped by
# the prompt that produced them (quality-plan §3.2 A/B).
SYNTH_PROMPT_VERSION = "synth-4"
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
    '- loop: {"id","type":"loop","over":"<expr e.g. context.items>","as":"<var>","body":"<body id>","max_iters":<int>}\n'
    '- browser: {"id","type":"browser","action":"eval|snapshot|handoff|complete","space":"<3-6 words>","code":"<ego helpers>","url":"<optional>","keep":true}\n\n'
    "Rules:\n"
    "- `entry` MUST be an existing node id; every edge endpoint MUST exist.\n"
    "- A loop's body returns to the loop head automatically: do NOT add an edge FROM the body; reference the loop item via {{<as>}}.\n"
    "- A branch routes via cases/default: do NOT add plain edges from a branch.\n"
    "- Put any per-run inputs the conversation revealed into context.schema + context.initial.\n"
    "- Default to a simple linear step chain; only add branch/loop when the trace clearly shows conditionals or repetition.\n"
    "- When the trace uses /browse, browser_eval, browser_snapshot, browser_handoff or "
    "logged-in page work, emit type:\"browser\" nodes (NOT a step that just says "
    "'use playwright'). action eval does the work script; login/captcha/payment is a "
    "separate action:\"handoff\" node; close/keep the Space with action:\"complete\" "
    "(complete MUST be its own node — never mix completeTaskSpace into the eval code). "
    "Reuse the same space name across the run. Prefer the logged-in embedded browser "
    "over mcp_playwright_* (that one is anonymous headless).\n"
    "- Agents: dev (code/actions), research (read/summarise), writer (draft text).\n"
    "- Any per-run placeholder you use in goal/prompt fields ({{variable_name}}) MUST also appear "
    'in context.schema.properties with type:"string" and in context.initial as "" '
    "so the caller can fill them before running.\n"
    # Dataflow contract (master-plan §2.2.9): steps that hand data to later steps
    # declare `writes`; the engine injects an implicit extractor that produces
    # strict JSON. The model must NOT be told to emit WRITE_JSON in its goal.
    "- When a step produces data a LATER step (or a loop) consumes, declare a "
    '`writes` field on that step mapping context keys to JSON Schema. Lists of '
    'items: {"type":"array","items":{"type":"object"}}. Single text: {"type":"string"}. '
    "The key name must match the {{context.key}} reference used downstream. "
    "Example: {\"id\":\"search\",\"type\":\"step\",\"agent\":\"research\","
    "\"goal\":\"Select the top stocks.\",\"writes\":{\"stocks\":{\"type\":\"array\",\"items\":{\"type\":\"object\"}}}}. "
    "A loop over such a list reads over:\"context.stocks\".\n"
    "- Do NOT put WRITE_JSON instructions in goals; the engine extracts structured "
    "output automatically from declared writes.\n\n"
    "Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _trace_text(messages, last_n: int | None = None) -> str:
    """Compact readable trace of a session for the synthesizer.

    Truncation strategy (workflow-ux-redesign S4a): file-read tool outputs are
    collapsed to their first lines (the full content never helps synthesis),
    other long outputs keep head + tail (endings carry the conclusion), plain
    messages keep their first 500 chars. ``last_n`` limits the trace to the
    most recent N messages (S5 range control)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    FILE_READ_TOOLS = {"read_file", "Read", "cat", "head", "tail", "view_file"}
    msgs = messages or []
    if last_n and last_n > 0:
        msgs = msgs[-last_n:]
    lines: list[str] = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            c = text_of_content(m.content)
            lines.append(f"USER: {c[:500]}")
        elif isinstance(m, AIMessage):
            c = text_of_content(m.content)
            if c.strip():
                lines.append(f"AGENT: {c[:500]}")
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"  -> tool {tc.get('name')}({json.dumps(tc.get('args') or {}, ensure_ascii=False)[:200]})")
        elif isinstance(m, ToolMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            name = getattr(m, "name", "tool") or "tool"
            if name in FILE_READ_TOOLS:
                preview = "\n".join(c.splitlines()[:3])[:200]
                lines.append(f"  <= {name}: {preview}… [file content elided]")
            elif len(c) > 400:
                lines.append(f"  <= {name}: {c[:200]}…[…]…{c[-100:]}")
            else:
                lines.append(f"  <= {name}: {c[:200]}")
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


async def _run_synthesis(trace: str, model, case_dir, usage_attr: dict | None = None) -> dict:
    """The ≤3-attempt self-correcting synthesis loop (shared by the live
    endpoint and offline replay). Returns a result dict::

        {"ok", "dsl", "raw", "errors", "fail_stage", "attempts_used", "total_ms"}

    Records each attempt onto ``case_dir`` when provided (quality-plan §3.1).
    Each model call is also logged to the global usage store (source=workflow,
    usage-stats-design §3.6) when ``usage_attr`` carries the attribution.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..workflows import synthesis as wf_synth
    from ..workflows.nodes import agent_helpers as ah

    extra_hint = ""
    dsl = None
    raw = ""
    errs: list[str] = ["no attempt made"]
    fail_stage = None
    t_start = time.time()
    attempt = 0
    for attempt in range(3):
        t0 = time.time()
        resp = await model.ainvoke(
            [SystemMessage(content=_SYNTHESIZE_PROMPT), HumanMessage(content=trace + extra_hint)]
        )
        ah.record_model_usage(resp, usage_attr)
        latency_ms = int((time.time() - t0) * 1000)
        raw = text_of_content(resp.content)
        dsl = _extract_json_obj(raw)
        if not isinstance(dsl, dict):
            extra_hint = (
                "\n\n[Previous attempt error: the reply was not a JSON object. "
                "Reply ONLY with a single {...} JSON object.]"
            )
            errs = ["model did not return a JSON DSL object"]
            fail_stage = "format.not_json"
            wf_synth.record_attempt(case_dir, attempt=attempt + 1, latency_ms=latency_ms,
                                    raw=raw, parse="not_json", validate_errors=[],
                                    hint_fed_back=extra_hint)
            continue
        dsl = wf_dsl.normalize_dsl(dsl)
        errs = wf_dsl.validate_dsl(dsl)
        wf_synth.record_attempt(
            case_dir, attempt=attempt + 1, latency_ms=latency_ms, raw=raw, parse="ok",
            validate_errors=list(errs),
            hint_fed_back=None if not errs else "[DSL errors: " + "; ".join(errs) + "]",
        )
        if not errs:
            fail_stage = None
            break
        fail_stage = "schema." + (errs[0].split(":")[0][:40] if errs else "unknown")
        extra_hint = (
            "\n\n[Previous attempt DSL errors: " + "; ".join(errs) +
            ". Fix them and reply ONLY with the corrected JSON object.]"
        )
    return {
        "ok": isinstance(dsl, dict) and not errs,
        "dsl": dsl if isinstance(dsl, dict) else None,
        "raw": raw,
        "errors": errs,
        "fail_stage": fail_stage,
        "attempts_used": max(1, attempt + 1),
        "total_ms": int((time.time() - t_start) * 1000),
    }


@router.post("/api/workflows/summarize-from-session")
async def summarize_session_to_dsl(data: dict) -> dict:
    """Distill a session's conversation into a workflow DSL *draft* (not saved).
    The UI then creates a workflow from it (version 1) or opens the dev agent."""
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
    last_n = (data or {}).get("last_n")
    last_n = last_n if isinstance(last_n, int) and last_n > 0 else None
    trace = _trace_text(messages, last_n)
    provider = (data or {}).get("provider") or prov_mod.get_default_provider()
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
        trace=trace,
        session_stats=session_stats,
        prompt_version=SYNTH_PROMPT_VERSION,
    )
    wf_synth.prune_cases()

    # S4b: up to 3 self-correcting attempts (shared helper, also used by replay).
    # Each attempt is metered into the usage log as source=workflow.
    synth_model_name = getattr(model, "model", None) or getattr(model, "model_name", "") or ""
    result = await _run_synthesis(trace, model, case_dir, usage_attr={
        "provider": provider,
        "model": synth_model_name,
        "session_id": session_id,
        "run_id": synthesis_id,
    })
    wf_synth.finish_case(
        case_dir,
        status="ok" if result["ok"] else "failed",
        dsl=result["dsl"],
        fail_stage=result["fail_stage"],
        total_latency_ms=result["total_ms"],
        attempts_used=result["attempts_used"],
    )
    if not result["ok"]:
        if result["dsl"] is None:
            return {"ok": False, "error": "model did not return a JSON DSL object",
                    "raw": result["raw"][:1000], "synthesis_id": synthesis_id}
        return {"ok": False,
                "error": "synthesized DSL invalid: " + "; ".join(result["errors"]),
                "dsl": result["dsl"], "synthesis_id": synthesis_id}
    return {"ok": True, "dsl": result["dsl"], "source_session_id": session_id,
            "synthesis_id": synthesis_id}


# ---- synthesis-case review API (quality-plan §3.2, UI in Settings) ----
@router.get("/api/synthesis/cases")
async def synthesis_cases_endpoint(limit: int = 100) -> dict:
    from ..workflows import synthesis as wf_synth

    return {"ok": True, "cases": wf_synth.list_cases(limit=max(1, min(500, limit)))}


@router.get("/api/synthesis/cases/{synthesis_id}")
async def synthesis_case_detail_endpoint(synthesis_id: str) -> dict:
    from ..workflows import synthesis as wf_synth

    case = wf_synth.load_case(synthesis_id)
    if not case:
        raise HTTPException(status_code=404, detail="synthesis case not found")
    return {"ok": True, "case": case}


@router.get("/api/synthesis/stats")
async def synthesis_stats_endpoint(days: int = 30) -> dict:
    """Aggregate funnel counts + top failure labels (quality-plan §3.2)."""
    from ..workflows import synthesis as wf_synth

    cutoff = time.time() - max(1, days) * 86400
    cases = wf_synth.list_cases(limit=500)
    total = l1 = l2 = l3 = 0
    edit_distances: list[int] = []
    fail_labels: dict[str, int] = {}
    for c in cases:
        if (c.get("ts") or 0) < cutoff:
            continue
        total += 1
        if c.get("status") == "ok":
            l1 += 1
        elif c.get("fail_stage"):
            fail_labels[c["fail_stage"]] = fail_labels.get(c["fail_stage"], 0) + 1
        oc = c.get("outcome") or {}
        if oc.get("created"):
            l2 += 1
        fr = oc.get("first_run") or {}
        if fr.get("status") == "done":
            l3 += 1
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


# ---- workflow execution (P2 engine) ----
def _wf_mcp_tools() -> list:
    try:
        return shared._mcp.all_langchain_tools() if shared._mcp else []
    except Exception:
        return []


def _wf_build_deps(run_id: str, workflow_id: str):
    """Resolve (wf, dsl, model, tools, fork_id, usage_attr) for a run by forking
    its source agent. Returns a 6-tuple of None when the workflow def is missing.

    ``usage_attr`` carries the attribution (provider/model/agent/run) the LLM
    nodes stamp on their per-call usage records (source=workflow); the driver
    adds ``session_id`` (present_in) before handing it to the engine.

    NOTE: fork_agent/build_model can RAISE (unknown agent, disabled/keyless
    provider) — callers must invoke this inside a try/except that marks the run
    failed, or the run is stranded in "running" forever.
    """
    wf = wf_store.get_def(workflow_id)
    if not wf:
        return None, None, None, None, None, None
    dsl = wf["dsl"]
    src_agent_id = None
    for n in dsl.get("nodes") or []:
        if n.get("agent"):
            src_agent_id = n["agent"]
            break
    src_agent_id = src_agent_id or wf.get("agent_id")
    # Fallback instead of fail when the referenced agent doesn't exist
    # (LLM-drafted DSLs invent role names — 2026-08-10 incident: the run
    # died at fork time before the first node). Same rule as AgentNode; a
    # warning event keeps the substitution visible. Only raises when NO
    # agent exists at all.
    from ..workflows.nodes import agent_helpers as ah

    resolved, agent_warn = ah.resolve_agent(src_agent_id)
    if resolved is None:
        raise ValueError(agent_warn or "no agents available")
    if agent_warn:
        _log.warning("workflow_run run=%s agent fallback: %s", run_id, agent_warn)
        wf_events.append_event(run_id, "warning", message=agent_warn)
    fork = agents_reg.fork_agent(resolved.id, f"wf-{run_id[:8]}-{resolved.id}")
    model = build_model(fork.provider, fork.model or None)
    tools = build_all_tools(_wf_mcp_tools())
    usage_attr = {
        "provider": fork.provider or "",
        "model": fork.model or getattr(model, "model", None) or getattr(model, "model_name", "") or "",
        "agent_id": resolved.id,
        "run_id": run_id,
    }
    return wf, dsl, model, tools, fork.id, usage_attr


def _set_run_status(
    run_id: str,
    status: str,
    error: str | None = None,
    only_from: tuple[str, ...] | None = None,
    error_detail: dict | None = None,
) -> None:
    """Persist a run status transition.

    ``error`` is stored when given and cleared on ``done``; terminal statuses
    stamp ``finished``. ``error_detail`` ({"node_id", "traceback"}) is the
    structured companion for localization UIs; it follows the same lifecycle
    as ``error``. ``only_from`` guards races (e.g. a queued engine "done"
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
    if error_detail is not None:
        run["error_detail"] = error_detail
    if status == "done":
        run["error"] = None
        run["error_detail"] = None
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


def _latest_session_id() -> str | None:
    """Most recently updated session across projects (design §9.6 headless bind)."""
    from ..session_meta import _session_meta_list

    best: tuple[float, str] | None = None
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            sid = m.get("id")
            if not sid:
                continue
            ts = float(m.get("updated") or m.get("created") or 0)
            if best is None or ts > best[0]:
                best = (ts, sid)
    return best[1] if best else None


def _bind_headless_browser_handoff(run_id: str, _interrupt: dict) -> str | None:
    """Attach a headless run to the latest session so handoff has a surface."""
    sid = _latest_session_id()
    if not sid:
        return None
    run = wf_store.get_run(run_id)
    if run:
        run["present_in_session_id"] = sid
        run["session_id"] = run.get("session_id") or sid
        run["updated"] = time.time()
        wf_storemod._write_json(wf_storemod._run_path(run_id), run)
    return sid


def _set_run_pending_interrupt(run_id: str, payload: dict | None) -> None:
    """Stamp/clear ``pending_interrupt`` on the run JSON (workflow-ux-redesign
    P1). Lets any UI (panel, dock badge) tell WHY a run is paused — a human
    question vs. a generic interrupt — without fetching events.jsonl. The
    payload mirrors the interrupt event (kind/node/question)."""
    run = wf_store.get_run(run_id)
    if not run:
        return
    run["pending_interrupt"] = payload
    run["updated"] = time.time()
    wf_storemod._write_json(wf_storemod._run_path(run_id), run)


def _backfill_first_run(workflow_id: str, run: dict) -> None:
    """Quality-plan §3.1: when a synthesized workflow's first run terminates,
    record the outcome on its synthesis case (L3 signal). Best-effort."""
    try:
        from ..workflows import synthesis as wf_synth

        meta = wf_storemod._read_meta(workflow_id) or {}
        syn = (meta.get("synthesized_from") or {}).get("synthesis_id")
        if not syn:
            return
        outcome = wf_synth.load_case(syn) or {}
        first_run = (outcome.get("outcome") or {}).get("first_run")
        if first_run:
            return  # only record the FIRST run
        wf_synth.backfill_outcome(
            wf_synth.find_case_by_synthesis_id(syn),
            first_run={
                "run_id": run.get("id"),
                "status": run.get("status"),
                "failed_node": (run.get("error_detail") or {}).get("node_id"),
            },
        )
    except Exception:
        pass


async def _mark_run_failed(run_id: str, exc: BaseException, present_in: str | None = None) -> None:
    """The one place a run failure is persisted: error event + run.error +
    ledger + run.status push (so chat/panel show the reason immediately).

    Driver-level failures (agent fork / model+tool build) have no engine
    events, so the traceback is captured here; node_id stays None because no
    node ever started."""
    from ..workflows.engine import _trimmed_traceback

    err = f"{type(exc).__name__}: {exc}"
    tb = _trimmed_traceback(exc)
    wf_events.append_event(run_id, "error", error=err, traceback=tb)
    _set_run_status(
        run_id, "failed", error=err, error_detail={"node_id": None, "traceback": tb}
    )
    sync_ledger.set_status(run_id, "failed", err)
    await _push_session_event(
        present_in, "run.status",
        {"run_id": run_id, "status": "failed", "error": err, "node_id": None},
    )


async def _drive_run_events(run_id: str, present_in: str | None, wf: dict, agen) -> None:
    """Persist + push each engine event; keep run step status + terminal state in sync."""
    # Build the node->step map from the RUN's steps (which include injected
    # ``<id>__extract`` steps; the definition's steps do not), so extract-node
    # events update their step and the run-status recomputation accounts for them.
    _run0 = wf_store.get_run(run_id) or {}
    node_to_step = {s["id"]: s["id"] for s in _run0.get("steps", [])}
    # loop node id -> body node id (to mark the body "skipped" when an
    # on_empty=skip loop completes with zero iterations; master-plan §2.1).
    loop_body: dict[str, str] = {}
    for _n in (wf.get("dsl") or {}).get("nodes") or []:
        if isinstance(_n, dict) and _n.get("type") == "loop" and _n.get("body"):
            loop_body[_n.get("id")] = _n["body"]
    last_interrupt: dict | None = None  # latest human-interrupt payload (P1)
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
        elif kind == "loop_skip":
            # on_empty=skip: the loop finished with zero iterations. The loop
            # node's own step lands "done" via its node_exit; its body never ran,
            # so mark the body step "skipped" (master-plan §2.1).
            body = loop_body.get(nid)
            if body and body in node_to_step:
                wf_store.update_step(run_id, node_to_step[body], "skipped")
        elif kind == "interrupt":
            # A node suspended the graph (human question or manual pause,
            # workflow-ux-redesign #14) — remember the payload so the "paused"
            # transition below can stamp it on the run JSON. nature
            # distinguishes the two; HumanNode events carry none and keep the
            # historical "human" default.
            last_interrupt = {k: v for k, v in ev.items() if k not in ("run_id", "kind")}
            last_interrupt["kind"] = ev.get("nature") or "human"
            _set_run_pending_interrupt(run_id, last_interrupt)
            # HumanNode suspends before its node_enter/exit events fire, so the
            # step would stay "pending" forever — mark it running while waiting.
            if nid in node_to_step:
                wf_store.update_step(run_id, node_to_step[nid], "running")
            # Design §9.6: a headless run (no present_in_session_id) that hits a
            # browser handoff must still have a surface — bind the latest session
            # so the pane / HandoffCard can open. Never leave an interrupt with
            # no picture.
            if last_interrupt.get("kind") == "browser_handoff" and not present_in:
                present_in = _bind_headless_browser_handoff(run_id, last_interrupt)
                if present_in:
                    await _push_session_event(
                        present_in,
                        "run.bind",
                        {
                            "run_id": run_id,
                            "workflow_id": wf.get("id"),
                            "present_in_session_id": present_in,
                        },
                    )
                    await _push_session_event(
                        present_in,
                        "browser.handoff",
                        {
                            "space": last_interrupt.get("space"),
                            "url": last_interrupt.get("url") or "",
                            "reason": last_interrupt.get("reason")
                            or last_interrupt.get("question")
                            or "",
                            "run_id": run_id,
                        },
                    )
        elif kind == "resume":
            last_interrupt = None
            _set_run_pending_interrupt(run_id, None)
            # A manually paused step RE-EXECUTES after resume (checkpoint
            # rewind) — its node_exit marks it done; don't flip it here.
            if nid in node_to_step and ev.get("nature") != "manual":
                wf_store.update_step(run_id, node_to_step[nid], "done")
        elif kind == "done":
            _set_run_pending_interrupt(run_id, None)
            _set_run_status(run_id, "done", only_from=("running", "paused"))
            await _push_session_event(present_in, "run.status", {"run_id": run_id, "status": "done"})
            _fin = wf_store.get_run(run_id)
            if _fin:
                _backfill_first_run(wf.get("id"), _fin)
        elif kind == "paused":
            _set_run_status(run_id, "paused", only_from=("running", "paused"))
            await _push_session_event(
                present_in, "run.status",
                {"run_id": run_id, "status": "paused", "pending_interrupt": last_interrupt},
            )
        elif kind == "error":
            # Mark the run failed FIRST: flipping a still-running step to
            # "failed" via update_step while the run were still "running" would
            # recompute it to "done" (all steps terminal) and the failed write
            # below would then be a no-op. With the run already "failed",
            # update_step only touches the step, not the run status.
            #
            # Extract-node attribution (master-plan §2.2.6): a failure raised by
            # a synthesized ``<src>__extract`` node is really the producing step's
            # failure — attribute error_detail.node_id to the source step so the
            # UI localizes it correctly, but keep the extract node id for detail.
            raw_nid = ev.get("node_id")
            attr_nid = raw_nid
            extract_nid = None
            if isinstance(raw_nid, str) and raw_nid.endswith("__extract"):
                extract_nid = raw_nid
                attr_nid = raw_nid[: -len("__extract")]
            _set_run_pending_interrupt(run_id, None)
            _set_run_status(
                run_id, "failed",
                error=str(ev.get("error") or ""),
                only_from=("running", "paused"),
                error_detail={
                    "node_id": attr_nid,
                    "extract_node_id": extract_nid,
                    "traceback": ev.get("traceback"),
                },
            )
            run = wf_store.get_run(run_id)
            if run:
                for s in run.get("steps", []):
                    # Fail the attributed step (and any still-running step).
                    if s.get("id") == attr_nid or s.get("status") == "running":
                        wf_store.update_step(run_id, s["id"], "failed")
            await _push_session_event(
                present_in, "run.status",
                {
                    "run_id": run_id,
                    "status": "failed",
                    "error": str(ev.get("error") or ""),
                    "node_id": attr_nid,
                },
            )
            _backfill_first_run(wf.get("id"), wf_store.get_run(run_id) or {})


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
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id)
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        if present_in:
            await _push_session_event(present_in, "run.bind", {"run_id": run_id, "workflow_id": workflow_id, "present_in_session_id": present_in})
        else:
            # Headless run (todo-sync et al.): there is no chat to carry run.*
            # events, and the completion push below only fires when the run
            # finishes. Announce the run at START too, so the Workflow panel
            # lists it as running immediately instead of materialising late.
            await _push_global_event("workflows.changed", {})
        agen = wf_engine.run_workflow(
            dsl, run_id=run_id, model=model, tools=tools,
            context_override=context_override,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
        )
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
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id)
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        _set_run_status(run_id, "running")
        # Manual pauses (#14) carry no node-side resume event — tell the engine
        # to emit it from the run's pending_interrupt nature instead.
        _nature = ((wf_store.get_run(run_id) or {}).get("pending_interrupt") or {}).get("kind")
        agen = wf_engine.resume_workflow(
            dsl, run_id=run_id, model=model, tools=tools, resume_value=resume_value,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
            resume_nature="manual" if _nature == "manual" else None,
        )
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
                if exc is not None:
                    from ..workflows.engine import _trimmed_traceback

                    err = f"{type(exc).__name__}: {exc}"
                    detail = {"node_id": None, "traceback": _trimmed_traceback(exc)}
                else:
                    err = "run task ended without emitting a terminal event"
                    detail = None
                wf_events.append_event(
                    run_id, "error", error=err,
                    traceback=(detail or {}).get("traceback"),
                )
                _set_run_status(run_id, "failed", error=err, error_detail=detail)
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
                last_ev = errs[-1]
                last = last_ev.get("error") or ""
                if last:
                    run["error"] = last
                    # Carry the diagnostic fields along too, so historical
                    # failed runs enjoy the same localization data.
                    if last_ev.get("traceback") or last_ev.get("node_id"):
                        run["error_detail"] = {
                            "node_id": last_ev.get("node_id"),
                            "traceback": last_ev.get("traceback"),
                        }
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
    _set_run_pending_interrupt(run_id, None)
    _set_run_status(run_id, "cancelled", error="cancelled by user")
    sync_ledger.set_status(run_id, "cancelled", "cancelled by user")
    await _push_session_event(run.get("present_in_session_id"), "run.status", {"run_id": run_id, "status": "cancelled"})
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
    decision = data.get("decision")
    if decision == "browser_resume":
        space = data.get("space") or (data.get("context_patch") or {}).get("space")
        if space:
            try:
                from ..browser import get_supervisor

                get_supervisor().take_over(space)
            except Exception:
                _log.exception("workflow browser take_over failed")
    value = {"decision": decision, "context_patch": data.get("context_patch") or {}}
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


async def _continue_run_bg(run_id: str, workflow_id: str, present_in: str | None = None) -> None:
    """Background driver for retry-from-checkpoint (P2): same never-silent-
    running invariant as the other drivers; streams the graph with input=None
    on the copied checkpoint so completed steps are not re-executed."""
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id)
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        _set_run_status(run_id, "running")
        agen = wf_engine.continue_workflow(
            dsl, run_id=run_id, model=model, tools=tools,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
        )
        await _drive_run_events(run_id, present_in, wf, agen)
        sync_ledger.set_status(run_id, "ok")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("workflow_continue_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        if fork_id:
            try:
                agents_reg.delete_agent(fork_id)
            except Exception:
                pass
        await _push_global_event("workflows.changed", {})


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
