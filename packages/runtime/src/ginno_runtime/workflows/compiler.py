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
    # Parallel-loop bodies (stability plan P3) extract inline per item inside
    # the gather adapter — no separate __extract node for them.
    from .dsl import loop_parallel_spec

    parallel_bodies = {
        n.get("body")
        for n in nodes
        if isinstance(n, dict) and n.get("type") == "loop" and loop_parallel_spec(n)[0]
    }
    existing_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
    synthetic = []
    for n in list(nodes):
        if not isinstance(n, dict) or not n.get("writes"):
            continue
        if n.get("id") in parallel_bodies:
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


def _inject_supervisor_gates(d: dict) -> dict:
    """Insert a ``<N>__sup`` gate after selected nodes when supervisor is
    enabled (design B §8.5). Runs AFTER extract injection so a gate follows
    ``<N>__extract`` when present. No-op when disabled — every existing DSL
    compiles to a byte-identical graph.

    Gate sources: nodes with a normal forward out-edge (or END). Loop bodies
    are excluded — their out-edge is the structural back-edge to the loop
    head, and a gate there would END the loop instead of gating it. Branch
    nodes route structurally via cases and are excluded the same way.
    ``checkpoints.after_nodes`` (explicit list) wins over ``every_step``."""
    sup = d.get("supervisor") or {}
    if not sup.get("enabled"):
        return d
    nodes = d.get("nodes") or []
    by_id = {n.get("id"): n for n in nodes if isinstance(n, dict)}
    loop_bodies = {
        n.get("body")
        for n in nodes
        if isinstance(n, dict) and n.get("type") == "loop" and n.get("body")
    }
    gateable_types = {"step", "agent", "llm", "human"}
    ck = sup.get("checkpoints") if isinstance(sup.get("checkpoints"), dict) else {}
    explicit = ck.get("after_nodes")
    if isinstance(explicit, list) and explicit:
        wanted = [n for n in explicit if isinstance(n, str)]
    else:  # every_step (default): every gateable non-body node
        wanted = [
            n.get("id")
            for n in nodes
            if isinstance(n, dict)
            and n.get("type") in gateable_types
            and n.get("id") not in loop_bodies
        ]
    existing = set(by_id)
    for src in wanted:
        if src in loop_bodies or by_id.get(src, {}).get("type") not in gateable_types:
            continue
        # effective predecessor: the extract node when one was injected
        pred = f"{src}__extract" if f"{src}__extract" in existing else src
        gate_id = f"{src}__sup"
        if gate_id in existing:  # defensive: id collision
            continue
        out = next((e for e in d.get("edges") or [] if e.get("from") == pred), None)
        nxt = out.get("to") if out else None
        if out:
            d["edges"].remove(out)
        d.setdefault("edges", []).append({"from": pred, "to": gate_id})
        nodes.append({
            "id": gate_id,
            "type": "supervisor_gate",
            "source_node": src,
            "continue_to": nxt,
        })
        existing.add(gate_id)
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
    # Supervisor gates ride on top of the extract layer (design B §8.5).
    d = _inject_supervisor_gates(d)

    cctx = {
        "dsl": d,
        "model": model,
        "tools": tools,
        "run_ctx": run_ctx,
        "supervisor": d.get("supervisor") or {},
        # Lets node adapters persist incremental bookkeeping (parallel gather
        # per-item progress) via put_writes and read it back on re-execution.
        "checkpointer": checkpointer,
    }
    g = StateGraph(WorkflowState)

    for n in d["nodes"]:
        cls = wf_nodes.get_or_raise(n["type"])
        g.add_node(n["id"], cls.make_node(n, cctx))

    for n in d["nodes"]:
        cls = wf_nodes.get_or_raise(n["type"])
        cls.add_edges(g, n, d)

    g.add_edge(START, d["entry"])
    return g.compile(checkpointer=checkpointer)
