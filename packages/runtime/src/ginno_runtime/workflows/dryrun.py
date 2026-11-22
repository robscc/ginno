"""Zero-LLM DSL preflight（stability plan P1d）.

normalize + validate + doctor + compile(stub model) + 入口可达性——
``POST /api/workflows/dry-run`` 与 dev agent 的 ``workflow_dry_run`` 工具共用
这一份实现（单一事实源，UI 与 agent 的检查口径永不漂移）。节点 callable 只
编译不执行：不需要 provider、不产生任何副作用，因此 dev agent 可以自主试跑
（对齐借鉴清单：允许自主 dry-run、禁止自主真跑）。
"""

from __future__ import annotations

from collections.abc import Iterator

from ..lang import t
from . import compiler as wf_compiler
from . import doctor as wf_doctor
from . import dsl as wf_dsl


class _DryRunModel:  # compile-time placeholder; never invoked here
    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        raise RuntimeError("dry-run stub model must never be invoked")


def _unbounded_cycle_count(d: dict) -> int:
    """Back-edge count over the user-level routing graph, loops excluded.

    A cycle is a real risk only when nothing bounds its iterations: loop
    head↔body edges are structural and capped by ``max_iters`` (the recursion
    budget already prices them in via ``engine._recursion_limit``), so they are
    deliberately left out of this graph. What remains — typically a branch
    case/default pointing back at an earlier node, which ``validate_dsl``
    intentionally allows — has no iteration bound: at run time only the
    recursion_limit stops it. That is legal-but-worth-flagging, exactly what
    the dry-run warning surface is for (branch back-edges must NOT be errors).
    """
    nodes = [n for n in (d.get("nodes") or []) if isinstance(n, dict)]
    idset = {n.get("id") for n in nodes}
    adj: dict[str, set[str]] = {}
    for e in (d.get("edges") or []) if isinstance(d.get("edges"), list) else []:
        if isinstance(e, dict) and e.get("from") in idset and e.get("to") in idset:
            adj.setdefault(e["from"], set()).add(e["to"])
    for n in nodes:
        if n.get("type") != "branch" or n.get("id") not in idset:
            continue  # loop head→body is structural (bounded); others use edges
        tgts = {
            c.get("then")
            for c in (n.get("cases") or [])
            if isinstance(c, dict) and c.get("then") in idset
        }
        if n.get("default") in idset:
            tgts.add(n["default"])
        adj.setdefault(n["id"], set()).update(tgts)

    # Iterative tri-color DFS from the entry: every gray→gray edge is a back
    # edge (one cycle through it). Unreachable cycles cannot fire and are the
    # `unreachable` field's business, not this counter's.
    entry = d.get("entry")
    if entry not in idset:
        return 0
    color: dict[str, int] = {}  # 1 = on stack (gray), 2 = done (black)
    back = 0
    color[entry] = 1
    stack: list[tuple[str, Iterator[str]]] = [
        (entry, iter(sorted(adj.get(entry, ()))))
    ]
    while stack:
        u, it = stack[-1]
        v = next(it, None)
        if v is None:
            color[u] = 2
            stack.pop()
            continue
        c = color.get(v, 0)
        if c == 1:
            back += 1
        elif c == 0:
            color[v] = 1
            stack.append((v, iter(sorted(adj.get(v, ())))))
    return back


def dry_run_dsl(dsl: dict) -> dict:
    """Preflight a DSL draft. Returns::

        {ok, errors, doctor_errors, warnings, unreachable, cycles,
         recursion_limit?, node_count?}

    ``ok`` is True only when validate+doctor+compile all pass; ``unreachable``
    lists nodes a BFS from START never visits (dead wiring). ``cycles`` counts
    unbounded routing cycles (branch back-edges etc.); when > 0 the run is
    only bounded by the reported ``recursion_limit`` and a non-blocking
    warning is appended for the panels/tool output."""
    d = wf_dsl.normalize_dsl(dsl)
    errors = wf_dsl.validate_dsl(d)
    doc = wf_doctor.run_doctor(d)
    result: dict = {
        "ok": False,
        "errors": errors,
        "doctor_errors": doc.get("errors") or [],
        "warnings": doc.get("warnings") or [],
        "unreachable": [],
    }
    if errors or result["doctor_errors"]:
        return result

    try:
        g = wf_compiler.compile_workflow(
            d, _DryRunModel(), [], {"run_id": "dry-run", "events": []}
        )
    except Exception as e:
        result["errors"] = [f"compile failed: {type(e).__name__}: {e}"]
        return result

    # Reachability: BFS from START over the compiled graph; anything unvisited
    # is dead wiring the author should know about before saving the draft.
    graph = g.get_graph()
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        adj.setdefault(e.source, []).append(e.target)
    seen = {"__start__"}
    stack = ["__start__"]
    while stack:
        cur = stack.pop()
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    result["unreachable"] = sorted(
        n for n in graph.nodes if n not in seen and n not in ("__start__", "__end__")
    )
    result["ok"] = True
    result["node_count"] = sum(
        1 for n in graph.nodes if n not in ("__start__", "__end__")
    )

    # Cycles (quick-fix D3): branch back-edges are legal DSL, so a cyclic
    # draft still passes everything above — but nothing bounds its iterations
    # at run time except the recursion_limit. Surface that as a warning (never
    # an error): the panels get structured fields for localized rendering and
    # the dev-agent tool gets the bilingual message inline.
    cycles = _unbounded_cycle_count(d)
    result["cycles"] = cycles
    if cycles:
        from .engine import _recursion_limit

        limit = _recursion_limit(d)
        result["recursion_limit"] = limit
        result["warnings"] = [
            *(result["warnings"] or []),
            {
                "rule": "graph.cycle",
                "node_id": "",
                "message": t(
                    f"Graph has {cycles} cycle(s) (branch back-edges are legal, "
                    f"not rejected); at run time only recursion_limit={limit} "
                    f"bounds it",
                    f"含循环依赖 {cycles} 处（branch 回边合法，不拒绝）；"
                    f"运行时仅靠 recursion_limit={limit} 兜底",
                ),
            },
        ]
    return result
