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
    yield {"run_id": run_id, "kind": "done"}
