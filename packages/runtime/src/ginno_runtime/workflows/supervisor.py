"""Supervisor: intervenes when a node's parameters/inputs fail validation (design A · round 3).

When a node is about to run and its params or edge-adapted input do not satisfy its
schemas, the node wrapper calls :func:`intervene`. The supervisor consults a
**pluggable decider** and returns one of:

* ``coerce``    — fill defaults / cast types; proceed with ``decision['input']``.
* ``patch_dsl`` — apply an in-memory patch to the node's params/transform; proceed.
* ``retry``     — proceed (treated like coerce for this pass).
* ``skip``      — do not execute the node; emit empty output and continue.
* ``abort``     — raise :class:`SupervisorAbort` (surfaces as an ``error`` event).

The default decider is deterministic and dependency-free: it attempts schema
coercion (defaults + casts); if that resolves every error it returns ``coerce``,
otherwise ``abort``. An LLM-backed or policy decider can be installed with
:func:`set_decider` — e.g. one that returns ``patch_dsl`` to rewrite the DSL or a
node's logic, per the product spec ("由 supervisor 决定修改 DSL 或修改节点逻辑").

Every intervention is recorded as a ``supervisor_intervene`` event (auditable).
"""

from __future__ import annotations

from typing import Any, Callable

Decider = Callable[[type, dict, list[str], dict, dict], dict]

_decider: Decider | None = None


class SupervisorAbort(Exception):
    """Raised when the supervisor decides a parameter failure cannot be recovered."""


def set_decider(fn: Decider | None) -> None:
    global _decider
    _decider = fn


def get_decider() -> Decider:
    return _decider or default_decider


def default_decider(node_cls: type, node: dict, errs: list[str], input_data: dict, cctx: dict) -> dict:
    """Deterministic recovery: coerce via schema defaults/casts, else abort."""
    coerced, remaining = node_cls.coerce_input(input_data)
    # Only the input-coercible errors are recoverable; param errors that remain after
    # coercing the node def are fatal here.
    param_errs = node_cls.validate_params(node)
    if not remaining and not param_errs:
        return {
            "action": "coerce",
            "input": coerced,
            "reason": "supervisor filled defaults / cast types: " + "; ".join(errs),
        }
    # Attempt param coercion too (defaults on params_schema).
    if param_errs and not remaining:
        from .nodes.base import coerce_against

        p_coerced, p_rem = coerce_against(node_cls.params_schema, node)
        if not p_rem:
            return {
                "action": "patch_dsl",
                "input": coerced,
                "patch": {"params": p_coerced},
                "reason": "supervisor patched node params: " + "; ".join(param_errs),
            }
    return {"action": "abort", "reason": "; ".join(remaining or param_errs or errs)}


def intervene(node_cls: type, node: dict, errs: list[str], input_data: dict, cctx: dict) -> dict:
    decider = get_decider()
    decision = decider(node_cls, node, errs, input_data, cctx) or {"action": "abort", "reason": "no decision"}
    # Apply an in-memory patch_dsl to this run's node def so execute sees fixed params.
    patch = decision.get("patch") or {}
    if decision.get("action") == "patch_dsl" and isinstance(patch.get("params"), dict):
        node.update(patch["params"])
    run_ctx = cctx.get("run_ctx")
    if run_ctx is not None:
        run_ctx["events"].append(
            {
                "run_id": run_ctx.get("run_id"),
                "node_id": node.get("id"),
                "kind": "supervisor_intervene",
                "action": decision.get("action"),
                "errors": errs,
                "reason": decision.get("reason", ""),
            }
        )
    return decision
