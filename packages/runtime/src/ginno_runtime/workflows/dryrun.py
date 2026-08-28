"""Zero-LLM DSL preflight（stability plan P1d）.

normalize + validate + doctor + compile(stub model) + 入口可达性——
``POST /api/workflows/dry-run`` 与 dev agent 的 ``workflow_dry_run`` 工具共用
这一份实现（单一事实源，UI 与 agent 的检查口径永不漂移）。节点 callable 只
编译不执行：不需要 provider、不产生任何副作用，因此 dev agent 可以自主试跑
（对齐借鉴清单：允许自主 dry-run、禁止自主真跑）。
"""

from __future__ import annotations

from . import compiler as wf_compiler
from . import doctor as wf_doctor
from . import dsl as wf_dsl


class _DryRunModel:  # compile-time placeholder; never invoked here
    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        raise RuntimeError("dry-run stub model must never be invoked")


def dry_run_dsl(dsl: dict) -> dict:
    """Preflight a DSL draft. Returns::

        {ok, errors, doctor_errors, warnings, unreachable, node_count?}

    ``ok`` is True only when validate+doctor+compile all pass; ``unreachable``
    lists nodes a BFS from START never visits (dead wiring)."""
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
    return result
