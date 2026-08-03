"""Compile a workflow DSL into a LangGraph StateGraph (design §5.1, refactored round 3).

Round 3 moved the per-type node logic into a pluggable, typed node system
(:mod:`ginno_runtime.workflows.nodes`). The compiler now only:

* normalizes + validates the DSL,
* asks the node **registry** for each node's class and calls ``make_node`` (which
  wraps every node with param/input validation, supervisor intervention, output
  recording and edge-transform propagation),
* asks each node class to wire its own edges (``add_edges``),
* compiles the graph for this run (per-run thread + checkpointer).

New node types (plugins) therefore compile with zero compiler changes.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from . import dsl as wf_dsl
from . import nodes as wf_nodes

# Back-compat re-exports (previously lived here).
from .nodes.agent_helpers import WRITE_OPEN, extract_write_json as _extract_write_json, parse_writes as _parse_writes  # noqa: F401


class WorkflowState(TypedDict):
    context: dict
    context_meta: dict
    results: dict
    loop_iters: dict
    loop_vars: dict  # loop-scoped vars (the `as` item); merged into goal rendering only
    events: Annotated[list, lambda a, b: (a or []) + (b or [])]
    inputs: dict  # node_id -> edge-transformed input for that node
    outputs: dict  # node_id -> typed output produced by that node


def compile_workflow(dsl: dict, model, tools: list, run_ctx: dict, checkpointer=None):
    """Compile a validated DSL into a StateGraph bound to this run's deps."""
    wf_nodes.load_plugins()
    d = wf_dsl.normalize_dsl(dsl)
    errs = wf_dsl.validate_dsl(d)
    if errs:
        raise ValueError("invalid DSL: " + "; ".join(errs))

    cctx = {"dsl": d, "model": model, "tools": tools, "run_ctx": run_ctx}
    g = StateGraph(WorkflowState)

    for n in d["nodes"]:
        cls = wf_nodes.get_or_raise(n["type"])
        g.add_node(n["id"], cls.make_node(n, cctx))

    for n in d["nodes"]:
        cls = wf_nodes.get_or_raise(n["type"])
        cls.add_edges(g, n, d)

    g.add_edge(START, d["entry"])
    return g.compile(checkpointer=checkpointer)
