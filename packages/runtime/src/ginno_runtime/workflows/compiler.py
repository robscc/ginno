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

from copy import deepcopy
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


def _inject_extract_nodes(dsl: dict) -> dict:
    """Synthesize an ``<id>__extract`` node after every node that declares
    ``writes`` (master-plan §2.2.4). Runs after validate_dsl (which never sees
    the synthetic nodes) and before graph construction. The stored DSL is never
    mutated — the injection is re-derived on every compile.

    Edge rewrite: every outgoing edge ``src -> X`` becomes ``src__extract -> X``
    plus a new ``src -> src__extract`` edge. Loop bodies are special: their
    back-edge is structural (added by LoopNode.add_edges), so instead of an
    explicit edge we record ``back_to=<loop head>`` on the extract node and
    LoopNode skips its own back-edge for such bodies.
    """
    d = deepcopy(dsl)
    nodes = d.get("nodes") or []
    loop_bodies = {
        n["body"]: n["id"]
        for n in nodes
        if isinstance(n, dict) and n.get("type") == "loop" and n.get("body")
    }
    existing_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    synthetic = []
    for n in list(nodes):
        if not isinstance(n, dict) or not n.get("writes"):
            continue
        src_id = n["id"]
        ext_id = f"{src_id}__extract"
        if ext_id in existing_ids:  # defensive: id collision
            continue
        ext_node = {
            "id": ext_id,
            "type": "extract",
            "source_node": src_id,
            "writes": n["writes"],
        }
        if n.get("extract_model"):
            ext_node["extract_model"] = n["extract_model"]
        if src_id in loop_bodies:
            ext_node["back_to"] = loop_bodies[src_id]
        nodes.append(ext_node)
        existing_ids.add(ext_id)
        synthetic.append(ext_id)
    # Redirect outgoing edges from a writes-declaring node to its extract node.
    writes_ids = {n["source_node"] for n in nodes
                  if isinstance(n, dict) and n.get("type") == "extract"}
    for e in d.get("edges") or []:
        if isinstance(e, dict) and e.get("from") in writes_ids:
            e["from"] = f"{e['from']}__extract"
    # Add src -> src__extract edges.
    for ext_id in synthetic:
        src = ext_id[: -len("__extract")]
        d.setdefault("edges", []).append({"from": src, "to": ext_id})
    d["nodes"] = nodes
    return d


def compile_workflow(dsl: dict, model, tools: list, run_ctx: dict, checkpointer=None):
    """Compile a validated DSL into a StateGraph bound to this run's deps."""
    wf_nodes.load_plugins()
    d = wf_dsl.normalize_dsl(dsl)
    errs = wf_dsl.validate_dsl(d)
    if errs:
        raise ValueError("invalid DSL: " + "; ".join(errs))

    # Inject implicit extract nodes for any step declaring ``writes`` (§2.2). The
    # injected nodes are validated-shaped (extract is a registered type) and are
    # re-derived each compile, so the stored DSL stays clean.
    d = _inject_extract_nodes(d)

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
