"""The ``python`` workflow node type: deterministic entries, no LLM.

DSL shape::

    {"id": "normalize", "type": "python", "entry": "normalize_and_compare",
     "args": {"model_name": "{{model_name}}",
              "aliyun_records": "{{context.aliyun_records}}", ...},
     "writes": {"comparison_summary": {"type": "object"}},
     "timeout": 300}

Security model: ``entry`` must name a key of
:data:`ginno_runtime.workflows.scripts.ENTRY_REGISTRY` (whitelist, validated
at DSL-validation time) — never arbitrary code. Entries run in a worker
thread (they may block on subprocess/CPU without stalling the sidecar loop)
under a node-level timeout.

``args`` resolution mirrors the other nodes' templating (see
:mod:`..expr`): a string that is exactly one ``{{expr}}`` placeholder is
evaluated to its RAW value (so arrays/objects flow through); any other
string is rendered as a template; non-strings pass through untouched.

``writes`` declares the output contract like on ``step`` nodes; the compiler
therefore injects a ``<id>__extract`` node after us — we put a
``WRITE_JSON {...}`` line into ``results[node_id]`` so that extractor takes
its deterministic fast path (no LLM call).
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from .. import expr as wf_expr
from ..scripts import ENTRY_REGISTRY
from .base import BaseNode
from .extract import _validate_writes
from .registry import register_node

# Backstop for entries that don't bound their own subprocess timeouts.
# Module-level so tests can monkeypatch it.
PYTHON_NODE_TIMEOUT_S = 600.0

_SINGLE_PLACEHOLDER = re.compile(r"^\s*\{\{(.*?)\}\}\s*$")


def _resolve_args(node: dict, render_ctx: dict) -> dict:
    out: dict = {}
    for key, val in (node.get("args") or {}).items():
        if isinstance(val, str):
            m = _SINGLE_PLACEHOLDER.match(val)
            if m:
                # Raw object passthrough: {{context.aliyun_records}} etc.
                try:
                    out[key] = wf_expr.eval_expr(m.group(1), render_ctx)
                except Exception:
                    out[key] = None
            else:
                out[key] = wf_expr.render(val, render_ctx)
        else:
            out[key] = val
    return out


@register_node
class PythonNode(BaseNode):
    """Run a whitelisted deterministic entry; write its result to context."""

    type = "python"
    params_schema = {
        "type": "object",
        "required": ["entry"],
        "properties": {
            "entry": {"type": "string"},
            "args": {"type": "object"},
            "timeout": {"type": "number"},
        },
    }

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        from .base import validate_against

        errs = validate_against(cls.params_schema, node)
        entry = node.get("entry")
        if isinstance(entry, str) and entry and not errs and entry not in ENTRY_REGISTRY:
            errs.append(
                f"node python: unknown entry '{entry}' "
                f"(registered: {', '.join(sorted(ENTRY_REGISTRY))})"
            )
        return errs

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        node_id = node["id"]
        entry_name = node.get("entry")
        context = dict(state.get("context") or {})
        events: list = []

        def emit(ev: dict) -> None:
            ev.setdefault("ts", time.time())
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "node_enter", "node_type": "python"})

        render_ctx = {**context, **(eff or {})}
        args = _resolve_args(node, render_ctx)
        timeout = float(node.get("timeout") or PYTHON_NODE_TIMEOUT_S)
        fn = ENTRY_REGISTRY[entry_name]

        t0 = time.time()
        try:
            # Worker thread: entries may block (subprocess/CPU) — never stall
            # the sidecar's event loop (chat streams, other runs).
            result = await asyncio.wait_for(asyncio.to_thread(fn, args), timeout=timeout)
        except TimeoutError:
            raise RuntimeError(
                f"python node '{node_id}' entry '{entry_name}' timed out after {timeout:.0f}s"
            ) from None
        except Exception as e:
            # Deterministic failure message lands in the run's error event.
            raise RuntimeError(f"python entry '{entry_name}' failed: {e}") from e

        writes_schema = node.get("writes") or {}
        if writes_schema:
            validated, errs = _validate_writes(result or {}, writes_schema)
            if errs:
                raise RuntimeError(
                    f"python entry '{entry_name}' output violates writes schema: "
                    + "; ".join(errs)
                )
        else:
            validated = dict(result or {})

        meta = dict(state.get("context_meta") or {})
        for k in validated:
            meta[k] = f"python:{node_id}"
        # Deterministic fast path for the compiler-injected extract node:
        # it adopts WRITE_JSON covering every declared key without an LLM call.
        result_text = f"WRITE_JSON {json.dumps(validated, ensure_ascii=False, default=str)}"
        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "context_write", "keys": list(validated.keys()), "method": "python"})
        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "node_exit", "status": "done",
              "duration_s": round(time.time() - t0, 2)})
        return {
            "context": {**context, **validated},
            "context_meta": meta,
            "results": {**(state.get("results") or {}), node_id: result_text},
            "events": events,
            "__output__": validated,
        }
