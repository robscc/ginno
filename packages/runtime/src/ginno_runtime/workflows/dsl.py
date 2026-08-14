"""Workflow DSL schema + validation + projections (design doc §3).

The DSL is the single source of truth for a workflow and compiles 1:1 to a
LangGraph graph (compiler lives in P2). This module is pure data + validation
so it can be unit-tested without the graph or the store.

v1 node types (decided Q1): step / branch / loop / human. `subflow` is parsed
but rejected by validate_dsl until v2. `loop.parallel` is accepted but ignored
in v1 (decided Q1).
"""

from __future__ import annotations

import re
from typing import Any

NODE_TYPES_V1 = {"step", "branch", "loop", "human", "browser"}
NODE_TYPES_ALL = NODE_TYPES_V1 | {"subflow"}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def validate_dsl(dsl: dict) -> list[str]:
    """Return a list of human-readable error strings (empty == valid).

    Checks: structure, entry exists & is a node, node ids unique, edges reference
    existing nodes, branch has default or >=1 case, loop has body + max_iters,
    step has goal, context.schema/initial are well-formed. Expression safety is
    enforced at compile/eval time (P2), not here.
    """
    errs: list[str] = []
    if not isinstance(dsl, dict):
        return ["dsl must be an object"]

    nodes = _as_list(dsl.get("nodes"))
    edges = _as_list(dsl.get("edges"))
    if not nodes:
        errs.append("at least one node is required")
    ids = [n.get("id") for n in nodes if isinstance(n, dict)]
    by_type = {n.get("id"): n.get("type") for n in nodes if isinstance(n, dict)}
    from . import nodes as wf_nodes  # lazy: dsl is imported by store at package init

    wf_nodes.load_plugins()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            errs.append(f"nodes[{i}] must be an object")
            continue
        nid = n.get("id")
        if not nid:
            errs.append(f"nodes[{i}] missing id")
        nt = n.get("type")
        if nt == "subflow":
            errs.append(f"node '{nid}' type 'subflow' is not supported until v2")
        elif nt == "extract":
            errs.append(
                f"node '{nid}' type 'extract' is compiler-internal — declare "
                f"`writes` on a step node instead"
            )
        elif wf_nodes.get_node(nt) is None:
            errs.append(f"node '{nid}' unknown type '{nt}'")
        # The compiler synthesizes <id>__extract nodes; users must not collide.
        if isinstance(nid, str) and nid.endswith("__extract"):
            errs.append(f"node '{nid}' id must not end with '__extract' (reserved)")
        # writes / extract_model shape (master-plan §2.2.3)
        w = n.get("writes")
        if w is not None:
            if not isinstance(w, dict) or not w:
                errs.append(f"node '{nid}' writes must be a non-empty object")
            else:
                for k, v in w.items():
                    if not isinstance(k, str) or not re.match(r"^[a-zA-Z0-9_]+$", k):
                        errs.append(f"node '{nid}' writes key '{k}' must match [a-zA-Z0-9_]+")
                    if not isinstance(v, dict) or "type" not in v:
                        errs.append(f"node '{nid}' writes['{k}'] must be an object with a 'type'")
        em = n.get("extract_model")
        if em is not None and not isinstance(em, str):
            errs.append(f"node '{nid}' extract_model must be a string")
    if len(ids) != len(set(ids)):
        errs.append("duplicate node id")
    idset = set(ids)
    # a loop's body returns to the loop head structurally; it must not carry its
    # own explicit out-edge (that would create an ambiguous second out-edge).
    loop_bodies = {n.get("body") for n in nodes if isinstance(n, dict) and n.get("type") == "loop"}
    loop_body_of = {n.get("id"): n.get("body") for n in nodes if isinstance(n, dict) and n.get("type") == "loop"}
    loop_out_count: dict[str, int] = {}

    entry = dsl.get("entry")
    if not entry:
        errs.append("entry is required")
    elif entry not in idset:
        errs.append(f"entry '{entry}' is not a node id")

    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            errs.append(f"edges[{i}] must be an object")
            continue
        f, t = e.get("from"), e.get("to")
        if f not in idset:
            errs.append(f"edges[{i}].from '{f}' unknown")
        if t not in idset:
            errs.append(f"edges[{i}].to '{t}' unknown")
        # branch routes structurally via cases/default — never an explicit edge.
        if by_type.get(f) == "branch":
            errs.append(f"edge from '{f}' not allowed (branch routes via cases/default; put a transform on a case)")
        # a loop's body back-edge is structural; but a loop MAY carry exactly ONE
        # explicit out-edge = its "done/next" continuation (so fetch→loop→gate works).
        if by_type.get(f) == "loop":
            if t == (loop_body_of.get(f)):
                errs.append(f"edge from '{f}' to its body not allowed (body returns to loop head)")
            else:
                loop_out_count[f] = loop_out_count.get(f, 0) + 1
                if loop_out_count[f] > 1:
                    errs.append(f"loop '{f}' may have at most one explicit out-edge (the done/next edge)")
        if f in loop_bodies:
            errs.append(f"edge from '{f}' not allowed (loop body returns to loop head)")
        tr = e.get("transform")
        if tr is not None and not isinstance(tr, dict):
            errs.append(f"edges[{i}].transform must be an object")
        elif isinstance(tr, dict) and "fn" in tr and not isinstance(tr.get("fn"), str):
            errs.append(f"edges[{i}].transform.fn must be a string")

    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid, nt = n.get("id"), n.get("type")
        cls = wf_nodes.get_node(nt)
        if cls is not None:
            errs.extend(cls.validate_params(n))
        # cross-node reference checks (need the full id set)
        if nt == "branch":
            for j, c in enumerate(_as_list(n.get("cases"))):
                if isinstance(c, dict) and c.get("then") and c["then"] not in idset:
                    errs.append(f"branch '{nid}' case[{j}].then '{c.get('then')}' unknown")
            if n.get("default") and n.get("default") not in idset:
                errs.append(f"branch '{nid}' default '{n.get('default')}' unknown")
        if nt == "loop":
            if n.get("body") and n["body"] not in idset:
                errs.append(f"loop '{nid}' body '{n.get('body')}' unknown")

    ctx = dsl.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict):
            errs.append("context must be an object")
        else:
            schema = ctx.get("schema")
            if schema is not None and not isinstance(schema, dict):
                errs.append("context.schema must be an object")
            initial = ctx.get("initial")
            if initial is not None and not isinstance(initial, dict):
                errs.append("context.initial must be an object")
    sup = dsl.get("supervisor")
    if sup is not None:
        if not isinstance(sup, dict):
            errs.append("supervisor must be an object")
        elif sup.get("enabled") and sup.get("mode") not in ("auto", "human"):
            errs.append("supervisor.mode must be 'auto' or 'human' when enabled")
    return errs


def steps_from_dsl(dsl: dict, include_extracts: bool = False) -> list[dict]:
    """Project nodes -> legacy `steps` view [{id,title,agent_id}] so existing
    consumers (workflow_* tools, right panel, chat WorkflowBlock) keep working
    until the P2 executor replaces them. Title falls back goal -> title -> id.

    ``include_extracts`` (run accounting, master-plan §2.2): for every node that
    declares ``writes`` the compiler injects a ``<id>__extract`` node. Runs must
    list those as steps too, otherwise the step-based run-status recomputation
    marks the run "done" the moment the producing step finishes — before the
    injected extract node has run (and possibly failed)."""
    out: list[dict] = []
    for n in _as_list(dsl.get("nodes")):
        if not isinstance(n, dict):
            continue
        out.append(
            {
                "id": n.get("id") or "",
                "title": n.get("title") or n.get("goal") or n.get("id") or "",
                "agent_id": n.get("agent") or n.get("agent_id"),
            }
        )
        if include_extracts and n.get("writes"):
            keys = list((n.get("writes") or {}).keys())
            out.append(
                {
                    "id": f"{n.get('id')}__extract",
                    "title": "提取结构化输出" + (f"（{'、'.join(keys)}）" if keys else ""),
                    "agent_id": None,
                }
            )
    return out


def legacy_steps_to_dsl(steps: list, name: str = "", description: str = "") -> dict:
    """Wrap an old {title,agent_id} steps array into a minimal linear DSL
    (one step node per entry, chained by edges) so legacy create/seed keep working."""
    steps = [s for s in _as_list(steps) if isinstance(s, dict)]
    nodes, edges = [], []
    for i, s in enumerate(steps):
        nid = s.get("id") or f"s{i + 1}"
        nodes.append(
            {
                "id": nid,
                "type": "step",
                "agent": s.get("agent_id") or s.get("agent"),
                "goal": s.get("goal") or s.get("title") or "",
                "title": s.get("title") or "",
            }
        )
        if i > 0:
            edges.append({"from": nodes[i - 1]["id"], "to": nid})
    return normalize_dsl(
        {
            "name": name,
            "description": description,
            "entry": nodes[0]["id"] if nodes else "",
            "nodes": nodes,
            "edges": edges,
        }
    )


def normalize_dsl(dsl: dict) -> dict:
    """Fill defaults so stored DSL is always well-shaped (id per node, dsl_version,
    context object). Does NOT validate — call validate_dsl separately."""
    d = dict(dsl or {})
    d.setdefault("dsl_version", "1")
    d.setdefault("name", "")
    d.setdefault("description", "")
    nodes = []
    for i, n in enumerate(_as_list(d.get("nodes"))):
        if not isinstance(n, dict):
            continue
        nn = dict(n)
        nn.setdefault("id", f"n{i + 1}")
        nn.setdefault("type", "step")
        nodes.append(nn)
    d["nodes"] = nodes
    d["edges"] = [dict(e) for e in _as_list(d.get("edges")) if isinstance(e, dict)]
    d.setdefault("entry", nodes[0]["id"] if nodes else "")
    ctx = d.get("context")
    if ctx is None:
        d["context"] = {"schema": {"type": "object", "properties": {}}, "initial": {}}
    else:
        ctx = dict(ctx)
        ctx.setdefault("schema", {"type": "object", "properties": {}})
        ctx.setdefault("initial", {})
        d["context"] = ctx
    d.setdefault("supervisor", {"enabled": False, "mode": "human"})
    return d


def canonical_dsl(dsl: dict) -> str:
    """Stable pretty JSON for diffing/versioning (sorted keys, no trailing noise)."""
    import json

    return json.dumps(normalize_dsl(dsl), indent=2, ensure_ascii=False, sort_keys=True)
