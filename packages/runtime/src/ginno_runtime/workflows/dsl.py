"""Workflow DSL schema + validation + projections (design doc §3).

The DSL is the single source of truth for a workflow and compiles 1:1 to a
LangGraph graph (compiler lives in P2). This module is pure data + validation
so it can be unit-tested without the graph or the store.

v1 node types (decided Q1): step / branch / loop / human (+ python,
registry-driven — validate_dsl accepts any registered node type). `subflow` is
parsed but rejected by validate_dsl until v2. `loop.parallel` (stability plan
P3) gathers all items in one body activation (asyncio.gather, index-ordered
array writes); the global gate ``settings.context.workflow_parallel_loops``
decides at run time whether it engages or degrades to sequential.
"""

from __future__ import annotations

import re
from typing import Any

NODE_TYPES_V1 = {"step", "branch", "loop", "human"}
NODE_TYPES_ALL = NODE_TYPES_V1 | {"subflow"}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


# Node types whose runtime body can fail in a retryable way (LLM / tool /
# browser execution). branch/human/pass/loop carry no such body: branch only
# routes, human suspends on interrupt (never retried), loop is a state machine
# — on_error on them would be meaningless (stability plan P2).
_ON_ERROR_TYPES = {"step", "agent", "llm", "browser"}


def _validate_fault_tolerance(n: dict, nid: str, errs: list[str]) -> None:
    """Shape-check the optional per-node fault-tolerance fields (stability
    plan P2): ``retry{max_attempts,backoff,backoff_ms}``, ``timeout_s`` and
    ``on_error``. Absent fields keep legacy behavior byte-for-byte (single
    attempt, global LLM timeout, hard stop on error) — ``normalize_dsl``
    deliberately injects no defaults for them."""
    r = n.get("retry")
    if r is not None:
        if not isinstance(r, dict):
            errs.append(f"node '{nid}' retry must be an object")
        else:
            ma = r.get("max_attempts")
            if ma is not None and (
                isinstance(ma, bool) or not isinstance(ma, int) or not 1 <= ma <= 10
            ):
                errs.append(f"node '{nid}' retry.max_attempts must be an integer in 1..10")
            bo = r.get("backoff")
            if bo is not None and bo not in ("fixed", "exponential"):
                errs.append(f"node '{nid}' retry.backoff must be 'fixed' or 'exponential'")
            bm = r.get("backoff_ms")
            if bm is not None and (
                isinstance(bm, bool) or not isinstance(bm, int) or not 0 <= bm <= 60000
            ):
                errs.append(f"node '{nid}' retry.backoff_ms must be an integer in 0..60000")
    ts = n.get("timeout_s")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0):
        errs.append(f"node '{nid}' timeout_s must be a number > 0")
    oe = n.get("on_error")
    if oe is not None:
        if oe not in ("stop", "continue"):
            errs.append(f"node '{nid}' on_error must be 'stop' or 'continue'")
        elif n.get("type") not in _ON_ERROR_TYPES:
            errs.append(
                f"node '{nid}' on_error is only supported on step/agent/llm/browser nodes"
            )


def loop_parallel_spec(loop_node: dict) -> tuple[bool, int]:
    """(parallel?, max_concurrency) for a loop node (stability plan P3).

    ``parallel`` accepts ``true`` (concurrency 4) or ``{"max_concurrency": N}``
    (1..8). Invalid shapes are validate_dsl's job; this helper clamps so the
    runtime never trusts the field blindly."""
    p = loop_node.get("parallel")
    if p is True:
        return True, 4
    if isinstance(p, dict):
        try:
            mc = int(p.get("max_concurrency") or 4)
        except (TypeError, ValueError):
            mc = 4
        return True, max(1, min(mc, 8))
    return False, 4


def parallel_loop_owner(dsl: dict, node_id: str) -> dict | None:
    """The parallel loop whose body is ``node_id``, if any (stability plan P3).

    Used by the runtime to switch a body step into gather mode and by the
    compiler/steps projection to skip the __extract injection for it."""
    for n in _as_list((dsl or {}).get("nodes")):
        if (
            isinstance(n, dict)
            and n.get("type") == "loop"
            and n.get("body") == node_id
            and loop_parallel_spec(n)[0]
        ):
            return n
    return None


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
    by_id = {n.get("id"): n for n in nodes if isinstance(n, dict)}
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
        # Same for the supervisor gates (design B §8.5).
        if isinstance(nid, str) and nid.endswith("__sup"):
            errs.append(f"node '{nid}' id must not end with '__sup' (reserved)")
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
        # Fault-tolerance fields (stability plan P2): all optional — absent
        # means byte-identical legacy behaviour (no retry, global timeout,
        # stop-on-error).
        _validate_fault_tolerance(n, nid, errs)
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

    # Multi-out-edge graphs were silently mis-routed pre-P2 (the compiler only
    # wired each node's FIRST out-edge). Strict mode (default on) rejects them
    # instead of shipping a graph that drops edges; the settings flag is the
    # rollback lever for any legacy DSL that relied on the silent behaviour.
    try:
        from .. import world_state as ws_mod

        strict_multi = bool(ws_mod.context_settings().get("workflow_strict_multi_edge", True))
    except Exception:
        strict_multi = True
    if strict_multi:
        out_count: dict[str, int] = {}
        for e in edges:
            if not isinstance(e, dict):
                continue
            f = e.get("from")
            if by_type.get(f) in ("branch", "loop"):
                continue  # branch out-edges are forbidden; loop capped below
            out_count[f] = out_count.get(f, 0) + 1
        for f, c in out_count.items():
            if c > 1:
                errs.append(
                    f"node '{f}' has {c} explicit out-edges; only linear chains are "
                    "supported (parallel fan-out is not implemented for this node type)"
                )

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
            # parallel loops (stability plan P3): the body runs as a gather over
            # all items, so its writes contract tightens to per-item appends.
            p = n.get("parallel")
            if p is not None:
                if p is not True and not isinstance(p, dict):
                    errs.append(f"loop '{nid}' parallel must be true or {{max_concurrency}}")
                elif isinstance(p, dict):
                    mc = p.get("max_concurrency", 4)
                    if isinstance(mc, bool) or not isinstance(mc, int) or not 1 <= mc <= 8:
                        errs.append(f"loop '{nid}' parallel.max_concurrency must be an int 1..8")
                body = by_id.get(n.get("body"))
                if isinstance(body, dict):
                    bcls = wf_nodes.get_node(body.get("type"))
                    btype = bcls.type if bcls is not None else body.get("type")
                    if btype != "agent":
                        errs.append(
                            f"loop '{nid}' is parallel but body '{body.get('id')}' is "
                            "not a step/agent node (only agent steps run per-item)"
                        )
                    bw = body.get("writes")
                    if not bw or not isinstance(bw, dict):
                        errs.append(
                            f"loop '{nid}' is parallel: body '{body.get('id')}' must "
                            "declare writes (per-item results are appended in index order)"
                        )
                    else:
                        for k, v in bw.items():
                            if not isinstance(v, dict) or v.get("type") != "array":
                                errs.append(
                                    f"loop '{nid}' is parallel: body writes key '{k}' "
                                    'must be {"type":"array"} (one element per item)'
                                )

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
        elif sup.get("enabled"):
            if sup.get("mode") not in ("auto", "human"):
                errs.append("supervisor.mode must be 'auto' or 'human' when enabled")
            errs.extend(_validate_supervisor_extra(sup))
    return errs


def _validate_supervisor_extra(sup: dict) -> list[str]:
    """Design B §8.5 supervisor configuration (only checked when enabled).

    ``checkpoints``: ``after_nodes`` (explicit id list) or ``every_step``
    (bool, the default when neither is set); ``on_error`` gates are declared
    but land with the auto adjudicator (P2.5). Budget fields (``retry_limit``,
    ``max_interventions``, ``token_budget``, ``confidence_min``) gate the AUTO
    path in P2.5 — validated now so a DSL that flips mode later doesn't break."""
    errs: list[str] = []
    ck = sup.get("checkpoints")
    if ck is not None:
        if not isinstance(ck, dict):
            errs.append("supervisor.checkpoints must be an object")
        else:
            if ck.get("every_step") is not None and not isinstance(ck.get("every_step"), bool):
                errs.append("supervisor.checkpoints.every_step must be a boolean")
            an = ck.get("after_nodes")
            if an is not None and (
                not isinstance(an, list) or not all(isinstance(x, str) for x in an)
            ):
                errs.append("supervisor.checkpoints.after_nodes must be a list of node ids")
            if ck.get("on_error") is not None and not isinstance(ck.get("on_error"), bool):
                errs.append("supervisor.checkpoints.on_error must be a boolean")
    for k in ("retry_limit", "max_interventions"):
        v = sup.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            errs.append(f"supervisor.{k} must be an integer >= 1")
    tb = sup.get("token_budget")
    if tb is not None and (isinstance(tb, bool) or not isinstance(tb, (int, float)) or tb <= 0):
        errs.append("supervisor.token_budget must be a positive number")
    cm = sup.get("confidence_min")
    if cm is not None and (
        isinstance(cm, bool) or not isinstance(cm, (int, float)) or not 0 <= cm <= 1
    ):
        errs.append("supervisor.confidence_min must be a number in 0..1")
    for k in ("policy", "model", "prompt"):
        v = sup.get(k)
        if v is not None and not isinstance(v, str):
            errs.append(f"supervisor.{k} must be a string")
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
    # Parallel-loop bodies extract inline per item (stability plan P3): the
    # compiler injects no __extract node for them, so the run step table must
    # not list one either (step table and graph must agree).
    parallel_bodies = {
        n.get("body")
        for n in _as_list(dsl.get("nodes"))
        if isinstance(n, dict) and n.get("type") == "loop" and loop_parallel_spec(n)[0]
    }
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
        if include_extracts and n.get("writes") and n.get("id") not in parallel_bodies:
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
