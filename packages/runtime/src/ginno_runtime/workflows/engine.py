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

from typing import Any, AsyncIterator

from langgraph.types import Command

from ..checkpointer import FileCheckpointer
from . import compiler as wf_compiler
from . import dsl as wf_dsl


async def run_workflow(
    dsl: dict,
    *,
    run_id: str,
    model,
    tools: list,
    context_override: dict | None = None,
    project_slug: str = "default",
) -> AsyncIterator[dict]:
    d = wf_dsl.normalize_dsl(dsl)
    initial = dict((d.get("context") or {}).get("initial") or {})
    if context_override:
        initial.update(context_override)
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": []}
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
        async for _mode, _payload in graph.astream(state, config=config, stream_mode=["updates"]):
            evs = run_ctx["events"]
            while yielded < len(evs):
                yield evs[yielded]
                yielded += 1
    except Exception as exc:  # surface graph/step failures as an event, don't crash
        ev = {"run_id": run_id, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}
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
        # A human/supervisor node suspended the graph — this is a *pause*, not an
        # error. The checkpoint is persisted; resume via resume_workflow().
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
) -> AsyncIterator[dict]:
    """Continue a paused run by resuming the persisted interrupt on its thread."""
    d = wf_dsl.normalize_dsl(dsl)
    run_ctx: dict[str, Any] = {"run_id": run_id, "events": []}
    graph = wf_compiler.compile_workflow(d, model, tools, run_ctx, checkpointer=FileCheckpointer(project_slug))
    config = {"configurable": {"thread_id": run_id}}
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
        ev = {"run_id": run_id, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}
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
