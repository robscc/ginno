"""Pluggable, typed workflow-node abstraction (design A · round 3).

A node type is a class with:
* ``type`` + ``aliases``      — how the DSL refers to it (``step`` is an alias of ``agent``).
* ``params_schema``           — JSON-Schema-ish spec for the node's own parameters;
  validated at DSL-validation time (``validate_dsl``) and again at run time.
* ``inputs_schema``/``outputs_schema`` — the typed ports used by edge transforms.
* ``validate_params``/``validate_input`` — parameter validation (per node, per input).
* ``execute``                 — the runtime body (returns a state-update dict; may set
  ``__output__`` which becomes the node's typed output for downstream transforms).
* ``add_edges``               — how the node wires itself into the LangGraph graph.

New node types are added by subclassing :class:`BaseNode` and decorating with
``@register_node`` (see :mod:`registry`) — the core never needs to change (decoupled).

The generic :meth:`BaseNode.make_node` wrapper provides, for *every* node type:
input resolution → parameter/input validation → **supervisor intervention** on
failure (coerce / patch_dsl / retry / skip / abort) → execute → output recording →
downstream input computation via edge ``transform``.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from . import transforms as wf_transforms

# Hard cap on a single workflow LLM call. Unlike the chat path (CHAT_TIMEOUT_S
# + the per-chunk stall watchdog in server._stream_graph), the workflow
# step/llm/extract nodes used to ``await`` the model with NO timeout, so a
# stalled provider hung the entire run forever — the task blocks on the call
# and the run is stranded in "running" (see wf_b4ee9936, 2026-08-10). With a
# timeout the node raises, the engine surfaces an error event, and the run
# lands "failed" instead of stuck. Module-level so tests can monkeypatch it.
WORKFLOW_LLM_TIMEOUT_S = 300.0


async def llm_invoke_with_timeout(coro, timeout: float | None = None):
    """Await a workflow LLM call, failing fast on a stalled provider.

    Raises a descriptive ``RuntimeError`` on timeout (caught by the engine and
    surfaced as an ``error`` event → the run is marked ``failed``).
    """
    t = WORKFLOW_LLM_TIMEOUT_S if timeout is None else timeout
    try:
        return await asyncio.wait_for(coro, timeout=t)
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"workflow LLM call timed out after {t:.0f}s (provider not responding)"
        ) from None

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def _check_schema(schema: dict, data: Any, path: str, errs: list[str]) -> None:
    """Minimal JSON-Schema walker (type/required/properties/items)."""
    if not isinstance(schema, dict) or not schema:
        return
    t = schema.get("type")
    if t and t in _TYPE_CHECKS and not _TYPE_CHECKS[t](data):
        errs.append(f"{path or '<root>'}: expected {t}, got {type(data).__name__}")
        return
    if t == "object" and isinstance(data, dict):
        for req in schema.get("required") or []:
            if req not in data or data[req] is None:
                errs.append(f"{path or '<root>'}: missing required '{req}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in data and data[key] is not None:
                _check_schema(sub, data[key], f"{path}.{key}" if path else key, errs)
    if t == "array" and isinstance(data, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, el in enumerate(data):
                _check_schema(items, el, f"{path}[{i}]", errs)


def _coerce_value(t: str, v: Any) -> Any:
    """Best-effort cast of ``v`` to schema type ``t``; raises on impossible casts."""
    if t in (None,) or _TYPE_CHECKS.get(t, lambda x: True)(v):
        return v
    if t == "string":
        return v if isinstance(v, str) else str(v)
    if t == "integer":
        return int(v)
    if t == "number":
        return float(v)
    if t == "boolean":
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if t == "array":
        return [v]
    if t == "object":
        if isinstance(v, dict):
            return v
        raise ValueError(f"cannot coerce {type(v).__name__} to object")
    return v


def validate_against(schema: dict, data: Any) -> list[str]:
    errs: list[str] = []
    _check_schema(schema or {}, data, "", errs)
    return errs


def coerce_against(schema: dict, data: Any) -> tuple[dict, list[str]]:
    """Fill schema defaults for missing keys + cast mistyped values; return
    (coerced, remaining_errors). Only top-level object properties are coerced."""
    out = dict(data or {})
    schema = schema or {}
    for key, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        if key not in out or out[key] is None:
            if "default" in sub:
                out[key] = sub["default"]
        else:
            try:
                out[key] = _coerce_value(sub.get("type"), out[key])
            except Exception:
                pass
    return out, validate_against(schema, out)


class BaseNode:
    """Base class for all workflow node types (built-in and plugin)."""

    type: ClassVar[str] = ""
    aliases: ClassVar[tuple] = ()
    params_schema: ClassVar[dict] = {"type": "object"}
    inputs_schema: ClassVar[dict] = {"type": "object"}
    outputs_schema: ClassVar[dict] = {"type": "object"}

    # ---------------- validation ---------------- #
    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        return validate_against(cls.params_schema, node)

    @classmethod
    def validate_input(cls, data: Any) -> list[str]:
        return validate_against(cls.inputs_schema, data)

    @classmethod
    def coerce_input(cls, data: Any) -> tuple[dict, list[str]]:
        return coerce_against(cls.inputs_schema, data)

    # ---------------- runtime ---------------- #
    @classmethod
    def make_node(cls, node: dict, cctx: dict):
        """Return the LangGraph node callable, wrapped with validation + supervisor + transforms."""
        from .. import supervisor as wf_sup

        nid = node["id"]

        async def wrapped(state: dict, config=None) -> dict:
            # Stamp the currently-executing node so engine-level error events
            # can attribute the failure (see engine.run_workflow's except).
            # Parallel supersteps would race this to "last starter" — current
            # DSLs execute sequentially, so that is acceptable for v1.
            cctx["run_ctx"]["current_node"] = nid
            ctx = dict(state.get("context") or {})
            inputs = dict(state.get("inputs") or {})
            eff = inputs.get(nid)
            if eff is None:
                eff = dict(ctx)
            errs = cls.validate_params(node) + cls.validate_input(eff)
            if errs:
                decision = wf_sup.intervene(cls, node, errs, eff, cctx)
                action = decision.get("action")
                if action == "abort":
                    raise wf_sup.SupervisorAbort(f"{nid}: {decision.get('reason', '')}")
                if action == "skip":
                    return cls._post(state, node, cctx, {})
                eff = decision.get("input", eff)
            update = await cls.execute(node, cctx, state, config, eff)
            output = update.pop("__output__", None) or {}
            node_inputs = update.pop("inputs", None)  # routing-time input adaptation (branch)
            post = cls._post(state, node, cctx, output)
            if node_inputs:
                post = {**post, "inputs": {**post["inputs"], **node_inputs}}
            return {**update, **post}

        return wrapped

    @staticmethod
    async def execute(node: dict, cctx: dict, state: dict, config, eff_input: dict) -> dict:
        raise NotImplementedError

    @classmethod
    def _post(cls, state: dict, node: dict, cctx: dict, output: dict) -> dict:
        """Record this node's output and compute downstream inputs via edge transforms."""
        d = cctx["dsl"]
        nid = node["id"]
        outputs = dict(state.get("outputs") or {})
        outputs[nid] = output
        inputs = dict(state.get("inputs") or {})
        ctx = dict(state.get("context") or {})
        for e in d.get("edges") or []:
            if e.get("from") == nid:
                inputs[e["to"]] = wf_transforms.apply_transform(e.get("transform"), output, ctx)
        return {"outputs": outputs, "inputs": inputs}

    # ---------------- graph wiring ---------------- #
    @staticmethod
    def _outgoing(d: dict, node_id: str) -> str | None:
        for e in d.get("edges") or []:
            if e.get("from") == node_id:
                return e.get("to")
        return None

    @classmethod
    def add_edges(cls, g, node: dict, d: dict) -> None:
        from langgraph.graph import END

        nxt = cls._outgoing(d, node["id"])
        g.add_edge(node["id"], nxt or END)
