"""Workflow execution engine (design §5).

``run_workflow`` is an async generator: it compiles the DSL (binding the run's
model + filtered toolset), seeds the WorkflowContext, streams the graph on a
per-run thread, and yields the events the step/branch nodes push into
``run_ctx['events']``. Persistence (events.jsonl + run step status) is the
caller's job (the server runner) so the engine stays pure and unit-testable.

Single-thread model (decided Q2): one compiled graph per run, steps share the
graph but keep separate message histories inside their node closures.
"""

from __future__ import annotations

import time
import traceback
from contextlib import contextmanager
from typing import Any, AsyncIterator

from langgraph.types import Command, interrupt

from ..checkpointer import FileCheckpointer
from . import compiler as wf_compiler
from . import dsl as wf_dsl


def _trimmed_traceback(exc: BaseException, max_chars: int = 4000) -> str:
    """Format ``exc``'s traceback, keeping only the tail when oversized.

    The tail frames are closest to the raise site and therefore the most
    useful for localization; the cap keeps run JSON / WS frames bounded.
    Reused by the API layer for driver-level failures (no engine events)."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) <= max_chars:
        return tb
    return "…[traceback trimmed — showing tail]…\n" + tb[-max_chars:]


# --------------------------------------------------------------------------- #
# Manual pause control channel (workflow-ux-redesign #14)
# --------------------------------------------------------------------------- #
# The API's POST /pause endpoint flags the LIVE run's run_ctx; the node wrapper
# (nodes/base.py) and the AgentNode tool-iteration boundary (nodes/builtin.py)
# check the flag via check_pause() and suspend via langgraph interrupt() — so
# the rewind/resume mechanics are identical to the human-node pause path and
# the checkpoint (persisted by FileCheckpointer) survives runtime restarts.
_RUN_CONTROLS: dict[str, dict] = {}  # run_id -> live run_ctx


def request_pause(run_id: str) -> bool:
    """Flag a RUNNING run to pause at the next safe boundary. Returns False
    when no live execution loop is registered for run_id (not started yet,
    already paused/finished, or the sidecar restarted)."""
    ctx = _RUN_CONTROLS.get(run_id)
    if ctx is None:
        return False
    ctx["pause_requested"] = True
    return True


def check_pause(run_ctx: dict, node_id: str) -> None:
    """Suspend the graph if a manual pause was requested (one-shot: the flag is
    cleared before suspending so the re-executed node on resume does not
    re-pause). Emits an interrupt event with nature="manual" so the API/UI can
    tell it apart from a human question, then raises GraphInterrupt via
    langgraph interrupt(). No-op when no pause was requested."""
    if not run_ctx.get("pause_requested"):
        return
    run_ctx.pop("pause_requested", None)
    run_ctx["events"].append({
        "ts": time.time(),
        "run_id": run_ctx["run_id"],
        "node_id": node_id,
        "kind": "interrupt",
        "nature": "manual",
    })
    interrupt({"kind": "manual", "node": node_id})


@contextmanager
def _run_control(run_id: str, run_ctx: dict):
    """Register run_ctx so request_pause() can reach it; identity-guarded pop
    so a finishing generator never clobbers a successor run's entry."""
    _RUN_CONTROLS[run_id] = run_ctx
    try:
        yield
    finally:
        if _RUN_CONTROLS.get(run_id) is run_ctx:
            _RUN_CONTROLS.pop(run_id, None)


async def run_workflow(
    dsl: dict,
    *,
    run_id: str,
    model,
    tools: list,
    context_override: dict | None = None,
    project_slug: str = "default",
    usage_attr: dict | None = None,
) -> AsyncIterator[dict]:
    d = wf_dsl.normalize_dsl(dsl)
    initial = dict((d.get("context") or {}).get("initial") or {})
    if context_override:
        initial.update(context_override)
    # usage_attr (provider/model/session/run attribution) is read by the LLM
    # nodes when they record per-call usage into the global usage log
    # (source=workflow, usage-stats-design §3.6).
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": [], "usage_attr": dict(usage_attr or {})}
    with _run_control(run_id, run_ctx):
        graph = wf_compiler.compile_workflow(
            d, model, tools, run_ctx, checkpointer=FileCheckpointer(project_slug)
        )

        state = {
            "context": initial,
            "context_meta": {k: "initial" for k in initial},
            "results": {},
            "loop_iters": {},
            "loop_vars": {},
            "events": [],
            "inputs": {},
            "outputs": {},
        }
        config = {"configurable": {"thread_id": run_id}}
        yielded = 0
        try:
            async for _mode, _payload in graph.astream(
                state, config=config, stream_mode=["updates"]
            ):
                evs = run_ctx["events"]
                while yielded < len(evs):
                    yield evs[yielded]
                    yielded += 1
        except Exception as exc:  # surface graph/step failures as an event, don't crash
            ev = {
                "run_id": run_id,
                "kind": "error",
                "error": f"{type(exc).__name__}: {exc}",
                # current_node is stamped by BaseNode.make_node (None → dropped by
                # append_event); traceback keeps error localization past the one-liner.
                "node_id": run_ctx.get("current_node"),
                "traceback": _trimmed_traceback(exc),
            }
            run_ctx["events"].append(ev)
            while yielded < len(run_ctx["events"]):
                yield run_ctx["events"][yielded]
                yielded += 1
            return
        # flush anything appended by the last node
        evs = run_ctx["events"]
        while yielded < len(evs):
            yield evs[yielded]
            yielded += 1
        # Defence in depth: workflow_* tools are now stripped from steps, but if the
        # graph is nonetheless paused on an interrupt, astream ends *normally* while
        # the step is incomplete — report an error instead of falsely marking the run
        # done (which would lose the step's output with no resume path).
        paused = False
        try:
            snap = await graph.aget_state(config)
            paused = bool(getattr(snap, "next", None)) or any(
                getattr(t, "interrupts", None) for t in (getattr(snap, "tasks", ()) or ())
            )
        except Exception:
            paused = False
        if paused:
            # A human/manual/supervisor interrupt suspended the graph — this is a
            # *pause*, not an error. The checkpoint is persisted; resume via
            # resume_workflow().
            ev = {"run_id": run_id, "kind": "paused"}
            run_ctx["events"].append(ev)
            yield ev
            return
        yield {"run_id": run_id, "kind": "done"}


async def run_state(run_id: str, dsl: dict, model, tools: list, project_slug: str = "default"):
    """Introspect whether a run is currently paused at an interrupt (for UI/status)."""
    d = wf_dsl.normalize_dsl(dsl)
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": []}
    graph = wf_compiler.compile_workflow(d, model, tools, run_ctx, checkpointer=FileCheckpointer(project_slug))
    config = {"configurable": {"thread_id": run_id}}
    try:
        snap = await graph.aget_state(config)
    except Exception:
        return {"paused": False}
    paused = bool(getattr(snap, "next", None)) or any(
        getattr(t, "interrupts", None) for t in (getattr(snap, "tasks", ()) or ())
    )
    return {"paused": paused, "next": list(getattr(snap, "next", ()) or ())}


async def resume_workflow(
    dsl: dict,
    *,
    run_id: str,
    model,
    tools: list,
    resume_value: dict,
    project_slug: str = "default",
    usage_attr: dict | None = None,
    resume_nature: str | None = None,
) -> AsyncIterator[dict]:
    """Continue a paused run by resuming the persisted interrupt on its thread.

    ``resume_nature``: set by the driver when the pending interrupt is NOT
    self-reported by a node (manual pause, workflow-ux-redesign #14). The
    interrupted node re-executes from scratch and this time skips its
    interrupt() call, so nothing would emit the resume event HumanNode would —
    the engine emits it here instead (``nature`` marks it so the API layer does
    not flip the re-executing step to "done")."""
    d = wf_dsl.normalize_dsl(dsl)
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": [], "usage_attr": dict(usage_attr or {})}
    with _run_control(run_id, run_ctx):
        graph = wf_compiler.compile_workflow(d, model, tools, run_ctx, checkpointer=FileCheckpointer(project_slug))
        config = {"configurable": {"thread_id": run_id}}
        if resume_nature:
            pending_node = None
            try:
                snap = await graph.aget_state(config)
                pending_node = next(iter(getattr(snap, "next", ()) or ()), None)
            except Exception:
                pass
            run_ctx["events"].append({
                "ts": time.time(),
                "run_id": run_id,
                "node_id": pending_node,
                "kind": "resume",
                "nature": resume_nature,
            })
        yielded = 0
        try:
            async for _mode, _payload in graph.astream(
                Command(resume=resume_value), config=config, stream_mode=["updates"]
            ):
                evs = run_ctx["events"]
                while yielded < len(evs):
                    yield evs[yielded]
                    yielded += 1
        except Exception as exc:
            ev = {
                "run_id": run_id,
                "kind": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "node_id": run_ctx.get("current_node"),
                "traceback": _trimmed_traceback(exc),
            }
            run_ctx["events"].append(ev)
            while yielded < len(run_ctx["events"]):
                yield run_ctx["events"][yielded]
                yielded += 1
            return
        evs = run_ctx["events"]
        while yielded < len(evs):
            yield evs[yielded]
            yielded += 1
        # After resuming, the graph may reach another interrupt (pause again) or finish.
        paused = (await run_state(run_id, d, model, tools, project_slug))["paused"]
        if paused:
            yield {"run_id": run_id, "kind": "paused"}
        else:
            yield {"run_id": run_id, "kind": "done"}


async def continue_workflow(
    dsl: dict,
    *,
    run_id: str,
    model,
    tools: list,
    project_slug: str = "default",
    usage_attr: dict | None = None,
) -> AsyncIterator[dict]:
    """Retry-from-failure (workflow-ux-redesign P2): stream the graph with
    input=None on the run's thread so LangGraph re-executes the pending node
    from the last committed superstep instead of re-running the whole graph.

    The caller copies the source run's checkpoint file under this run's id
    before starting; if the checkpoint is missing/empty the graph has nothing
    to continue and this yields an error event."""
    d = wf_dsl.normalize_dsl(dsl)
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": [], "usage_attr": dict(usage_attr or {})}
    with _run_control(run_id, run_ctx):
        graph = wf_compiler.compile_workflow(d, model, tools, run_ctx, checkpointer=FileCheckpointer(project_slug))
        config = {"configurable": {"thread_id": run_id}}
        yielded = 0
        try:
            snap = await graph.aget_state(config)
            has_state = snap is not None and (
                bool(getattr(snap, "next", None))
                or bool((getattr(snap, "values", None) or {}))
            )
        except Exception:
            has_state = False
        if not has_state:
            yield {
                "run_id": run_id,
                "kind": "error",
                "error": "no resumable checkpoint for this run (retry from the start instead)",
                "node_id": None,
            }
            return
        try:
            async for _mode, _payload in graph.astream(
                None, config=config, stream_mode=["updates"]
            ):
                evs = run_ctx["events"]
                while yielded < len(evs):
                    yield evs[yielded]
                    yielded += 1
        except Exception as exc:
            ev = {
                "run_id": run_id,
                "kind": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "node_id": run_ctx.get("current_node"),
                "traceback": _trimmed_traceback(exc),
            }
            run_ctx["events"].append(ev)
            while yielded < len(run_ctx["events"]):
                yield run_ctx["events"][yielded]
                yielded += 1
            return
        evs = run_ctx["events"]
        while yielded < len(evs):
            yield evs[yielded]
            yielded += 1
        paused = (await run_state(run_id, d, model, tools, project_slug))["paused"]
        if paused:
            yield {"run_id": run_id, "kind": "paused"}
        else:
            yield {"run_id": run_id, "kind": "done"}
