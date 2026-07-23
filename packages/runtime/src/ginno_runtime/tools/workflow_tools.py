"""Workflow tools — let agents run a workflow as a tracked, step-by-step
process (right-panel Workflow tab shows the live progress tree).

The tools return concise text that includes the run_id (and step ids on
run) so the WS layer can parse the run_id and push a `workflow.emit`
snapshot to the chat + tab without dumping JSON into the model context.
A module-level cache holds the latest snapshot per run_id for the live
turn (runs themselves persist on disk).
"""

from __future__ import annotations

import difflib
import json

from langchain_core.tools import tool
from langgraph.types import interrupt

from .. import workflows as wf_store
from ..workflows import dsl as wf_dsl

WORKFLOW_TOOL_NAMES = {"workflow_list", "workflow_create", "workflow_run", "workflow_step"}
# Gated to the workflow-dev agent (P5): editing tools that pause for a diff
# confirmation via interrupt before mutating a versioned definition.
WORKFLOW_DEV_TOOL_NAMES = {"workflow_propose_edit"}

# run_id -> latest run snapshot (for the live-turn inline workflow block)
RUN_CACHE: dict[str, dict] = {}


def _steps_text(run: dict) -> str:
    return "; ".join(f"[{s['id']}] {s['title']} ({s['status']})" for s in run.get("steps", []))


@tool
def workflow_list() -> str:
    """List available workflow definitions (id, name, step count)."""
    defs = wf_store.list_defs()
    if not defs:
        return "(no workflows defined)"
    return "\n".join(f"[{d['id']}] {d['name']} — {len(d.get('steps', []))} steps" for d in defs)


@tool
def workflow_create(name: str, description: str, steps_json: str) -> str:
    """Create a workflow definition. steps_json = JSON array of {"title", "agent_id"?}."""
    try:
        steps = json.loads(steps_json) if steps_json else []
    except json.JSONDecodeError:
        return "error: steps_json is not valid JSON"
    if not isinstance(steps, list):
        return "error: steps_json must be a JSON array"
    wf = wf_store.create_def({"name": name, "description": description, "steps": steps})
    return f"created workflow [{wf['id']}] '{wf['name']}' with {len(wf['steps'])} steps"


@tool
def workflow_run(workflow_id: str = "", name: str = "") -> str:
    """Start a run of a workflow (by id or name). Returns run_id + step ids."""
    wf = None
    if workflow_id:
        wf = wf_store.get_def(workflow_id)
    if not wf and name:
        for d in wf_store.list_defs():
            if d.get("name", "").lower() == name.lower():
                wf = d
                break
    if not wf:
        return "error: workflow not found (use workflow_list to see ids)"
    run = wf_store.create_run(wf)
    RUN_CACHE[run["id"]] = run
    return (
        f"started run_id={run['id']} for workflow '{run['name']}' "
        f"({len(run['steps'])} steps): {_steps_text(run)}"
    )


@tool
def workflow_step(run_id: str, step_id: str, status: str, output: str = "") -> str:
    """Mark a workflow run's step as running/done/failed. output optional."""
    if status not in ("running", "done", "failed"):
        status = "done"
    run = wf_store.update_step(run_id, step_id, status, output)
    if not run:
        return f"error: run {run_id} not found"
    RUN_CACHE[run_id] = run
    done = sum(1 for s in run["steps"] if s["status"] in ("done", "failed"))
    return (
        f"run_id={run_id} step [{step_id}] -> {status} "
        f"({done}/{len(run['steps'])} steps complete)"
    )


ALL_WORKFLOW_TOOLS = [workflow_list, workflow_create, workflow_run, workflow_step]


@tool
def workflow_propose_edit(workflow_id: str, new_dsl_json: str, rationale: str = "") -> str:
    """Propose a new DSL version for a workflow. PAUSES for a human diff
    confirmation (independent of the permission system): the UI shows a unified
    diff with Apply/Reject. Only creates a new immutable version when the human
    applies. new_dsl_json = the full proposed DSL object as JSON.

    Returns the outcome text (new version number on apply, or the rejection)."""
    try:
        new_dsl = json.loads(new_dsl_json) if isinstance(new_dsl_json, str) else new_dsl_json
    except json.JSONDecodeError:
        return "error: new_dsl_json is not valid JSON"
    if not isinstance(new_dsl, dict):
        return "error: new_dsl must be a JSON object"
    new_dsl = wf_dsl.normalize_dsl(new_dsl)
    errs = wf_dsl.validate_dsl(new_dsl)
    if errs:
        return "error: proposed DSL invalid: " + "; ".join(errs)

    cur = wf_store.get_def(workflow_id)
    if not cur:
        return f"error: workflow {workflow_id} not found"
    cur_dsl = cur.get("dsl") or {}
    diff = "".join(
        difflib.unified_diff(
            wf_dsl.canonical_dsl(cur_dsl).splitlines(keepends=True),
            wf_dsl.canonical_dsl(new_dsl).splitlines(keepends=True),
            fromfile=f"v{cur.get('version', 0)}",
            tofile="proposed",
        )
    )

    # Pause the graph: the server emits a `version.propose` event from this
    # interrupt and resumes with {"decision": "allow"|"deny"} (the tool's value).
    decision = interrupt(
        {
            "kind": "version_propose",
            "workflow_id": workflow_id,
            "from_version": cur.get("version", 0),
            "diff": diff,
            "rationale": rationale,
        }
    )
    if isinstance(decision, dict) and decision.get("decision") == "allow":
        wf = wf_store.update_def(
            workflow_id, {"dsl": new_dsl, "commit": rationale or "via workflow-dev agent"}
        )
        ver = wf.get("version") if wf else "?"
        return f"applied: workflow '{workflow_id}' is now version {ver}."
    return "rejected: the human rejected this edit. Ask what to change and propose again."


ALL_WORKFLOW_DEV_TOOLS = [workflow_propose_edit]
