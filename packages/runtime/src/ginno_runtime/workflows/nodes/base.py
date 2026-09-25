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
import time
from typing import Any, ClassVar

from . import transforms as wf_transforms

# Backoff ceiling for per-node retries (stability plan P2): exponential
# backoff doubles per attempt and is capped here so a 10-attempt policy can
# never sleep for minutes inside a single node.
_MAX_RETRY_BACKOFF_MS = 30_000

# Hard cap on a single workflow LLM call. Unlike the chat path (CHAT_TIMEOUT_S
# + the per-chunk stall watchdog in server._stream_graph), the workflow
# step/llm/extract nodes used to ``await`` the model with NO timeout, so a
# stalled provider hung the entire run forever — the task blocks on the call
# and the run is stranded in "running" (see wf_b4ee9936, 2026-08-10). With a
# timeout the node raises, the engine surfaces an error event, and the run
# lands "failed" instead of stuck. Module-level so tests can monkeypatch it.
WORKFLOW_LLM_TIMEOUT_S = 300.0

# Hard cap on ONE tool batch inside a workflow agent step (2026-09-25: an
# agent's glob_files with reachable root "/" walked the entire filesystem —
# the node never returned and the run sat in "running" forever). Bounded here
# the node fails with an attributable timeout error → run "failed" → the
# existing recovery paths (node retry, retry_from_checkpoint, rerun_from)
# apply. Overridable per node via ``tool_timeout_s``.
WORKFLOW_TOOL_TIMEOUT_S = 300.0


class _BoundedTimeout(Exception):
    """Internal marker: the bounded await REALLY timed out (never used for
    errors raised by the awaited call itself — those must propagate as-is)."""


async def _await_bounded(coro, timeout: float, what: str):
    """Await with a timeout that ALWAYS raises, even if the inner swallows
    cancellation (an httpx/openai client stuck on a dead socket can trap
    CancelledError, which would leave ``asyncio.wait_for`` hanging forever —
    the exact 假死 of 2026-09-25). Cancels, waits a short grace for the
    cancellation to land, then raises regardless of the inner's state."""
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=5.0)  # grace for a polite cancel
    except Exception:
        pass  # raising below is the point; a stuck task is abandoned
    raise _BoundedTimeout(f"{what} timed out after {timeout:.0f}s")


async def llm_invoke_with_timeout(coro, timeout: float | None = None):
    """Await a workflow LLM call, failing fast on a stalled provider.

    Raises a descriptive ``RuntimeError`` on timeout (caught by the engine and
    surfaced as an ``error`` event → the run is marked ``failed``).
    """
    t = WORKFLOW_LLM_TIMEOUT_S if timeout is None else timeout
    try:
        return await _await_bounded(coro, t, "workflow LLM call")
    except _BoundedTimeout:
        raise RuntimeError(
            f"workflow LLM call timed out after {t:.0f}s (provider not responding)"
        ) from None


async def tools_invoke_with_timeout(coro, timeout: float | None = None):
    """Await one workflow tool batch under ``WORKFLOW_TOOL_TIMEOUT_S``."""
    t = WORKFLOW_TOOL_TIMEOUT_S if timeout is None else timeout
    try:
        return await _await_bounded(coro, t, "workflow tool batch")
    except _BoundedTimeout:
        raise RuntimeError(
            f"workflow tool batch timed out after {t:.0f}s "
            "(tool hung — unbounded filesystem walk / dead external call?)"
        ) from None

def _overlay_patch(inputs: dict, patch: dict) -> dict:
    """Overlay a resume context_patch onto the inputs channel: every input
    snapshot (dict) that already carries a patched key sees the patched value.
    Keys the snapshot doesn't carry are left alone — nodes render missing keys
    from context, which the patch already reached."""
    out = {}
    for k, v in (inputs or {}).items():
        hit = [pk for pk in patch if isinstance(v, dict) and pk in v]
        out[k] = {**v, **{pk: patch[pk] for pk in hit}} if hit else v
    return out


def _apply_resume_patch(state: dict, patch: dict) -> dict:
    """State as the resumed node should see it: context patched + the inputs
    channel's stale pre-pause snapshots overlaid (see wrapped/_with_patch)."""
    ctx = {**(state.get("context") or {}), **patch}
    return {
        **state,
        "context": ctx,
        "inputs": _overlay_patch(state.get("inputs") or {}, patch),
    }


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
            # Manual-pause boundary (workflow-ux-redesign #14): suspend BEFORE
            # this node starts — the checkpoint rewinds to the last committed
            # superstep, so resume simply executes this node (no duplicated
            # work). Function-local import: engine imports compiler which
            # imports this module.
            from .. import engine as wf_engine

            # Replay after a manual-pause resume returns the resume value's
            # context_patch (design B P2) — see engine.check_pause. The patch
            # must reach THREE places: context, this node's effective input
            # (eff comes from the checkpoint's inputs channel, persisted
            # pre-pause), and the successor inputs computed by _post — the
            # studio e2e suite proved a context-only merge is invisible to a
            # downstream node whose input snapshot shadows it.
            patch = wf_engine.check_pause(cctx["run_ctx"], nid)
            if patch:
                state = _apply_resume_patch(state, patch)
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
                    return _with_patch(cls._post(state, node, cctx, {}), patch, state)
                eff = decision.get("input", eff)
            update = await cls._execute_resilient(node, cctx, state, config, eff)
            if update is None:
                # on_error="continue" soft failure (stability plan P2): the error
                # event was emitted with handled:true and soft_failures bumped;
                # proceed with an empty output so downstream still runs and the
                # run can finish "done" (with a warnings count).
                return _with_patch(cls._post(state, node, cctx, {}), patch, state)
            output = update.pop("__output__", None) or {}
            node_inputs = update.pop("inputs", None)  # routing-time input adaptation (branch)
            post = cls._post(state, node, cctx, output)
            if node_inputs:
                post = {**post, "inputs": {**post["inputs"], **node_inputs}}
            return _with_patch({**update, **post}, patch, state)

        def _with_patch(final: dict, patch: dict | None, patched_state: dict) -> dict:
            """Commit a manual-resume context_patch even when the node's own
            update doesn't touch context (branch/pass), and make the patch WIN
            over the node's own re-derived context. The win matters when the
            resumed node is a synthesized <src>__extract re-committing its
            PRE-pause extraction: the user's explicit patch (「改 context 后
            继续」, same-key-overwrites semantics as the decision card) must not
            be clobbered by the stale value. Successor inputs computed by _post
            get the same overlay for the same reason."""
            if not patch:
                return final
            base = dict(patched_state.get("context") or {})
            merged = {**base, **(final.get("context") or {}), **patch}
            out = {**final, "context": merged}
            if isinstance(out.get("inputs"), dict):
                out["inputs"] = _overlay_patch(out["inputs"], patch)
            return out

        return wrapped

    @classmethod
    async def _execute_resilient(cls, node: dict, cctx: dict, state: dict, config, eff: dict):
        """``execute`` under the node's optional fault-tolerance policy
        (stability plan P2): ``retry{max_attempts,backoff,backoff_ms}`` retries
        with backoff, ``timeout_s`` bounds total execution time, and
        ``on_error`` decides whether an exhausted failure stops the run
        (``stop``, default = re-raise, engine marks the run failed) or soft-
        fails this node only (``continue`` = emit ``error`` with
        ``handled:true``, count it in ``run_ctx['soft_failures']``, return
        ``None`` so the caller proceeds with an empty output).

        Absent fields keep legacy behavior byte-for-byte: one attempt, no
        wall-clock cap, re-raise on failure.

        Control-flow exceptions are NEVER retried: ``CancelledError`` (cooperative
        cancel), ``GraphInterrupt`` (human node / manual pause suspensions — note
        it IS an ``Exception`` subclass in langgraph, so it must be re-raised
        ahead of the generic handler), ``SupervisorAbort`` and
        ``GraphRecursionError`` (graph-level failures).
        """
        from langgraph.errors import GraphInterrupt, GraphRecursionError

        from .. import engine as wf_engine
        from .. import supervisor as wf_sup

        run_ctx = cctx["run_ctx"]
        nid = node["id"]
        spec = node.get("retry") or {}
        try:
            max_attempts = max(1, min(int(spec.get("max_attempts") or 1), 10))
        except (TypeError, ValueError):
            max_attempts = 1
        backoff = spec.get("backoff") or "fixed"
        try:
            backoff_ms = int(spec.get("backoff_ms") or 1000)
        except (TypeError, ValueError):
            backoff_ms = 1000
        timeout_s = node.get("timeout_s")
        on_error = node.get("on_error") or "stop"
        # Parallel-loop bodies apply retry/on_error PER ITEM inside the gather
        # adapter; a node-level wrap would multiply attempts (items × attempts)
        # and contradict per-item decisions. timeout_s still bounds the batch.
        try:
            from ..dsl import parallel_loop_owner

            if parallel_loop_owner(cctx.get("dsl") or {}, nid):
                max_attempts = 1
                on_error = "stop"
        except Exception:
            pass

        def _delay_seconds(attempt: int) -> float:
            ms = backoff_ms if backoff != "exponential" else backoff_ms * (2 ** (attempt - 1))
            return min(ms, _MAX_RETRY_BACKOFF_MS) / 1000.0

        attempt = 1
        last_exc: BaseException | None = None
        while True:
            try:
                coro = cls.execute(node, cctx, state, config, eff)
                if timeout_s:
                    return await asyncio.wait_for(coro, timeout=float(timeout_s))
                return await coro
            except (
                asyncio.CancelledError,
                GraphInterrupt,
                GraphRecursionError,
                wf_sup.SupervisorAbort,
            ):
                raise
            except Exception as exc:  # noqa: BLE001 — deliberate retry boundary
                if isinstance(exc, asyncio.TimeoutError):
                    last_exc = RuntimeError(
                        f"node '{nid}' timed out after {timeout_s}s (on_error/retry policy applies)"
                    )
                else:
                    last_exc = exc
            if attempt >= max_attempts:
                if on_error == "continue":
                    run_ctx["soft_failures"] = int(run_ctx.get("soft_failures") or 0) + 1
                    run_ctx["events"].append(
                        {
                            "ts": time.time(),
                            "run_id": run_ctx.get("run_id"),
                            "node_id": nid,
                            "kind": "error",
                            "error": f"{type(last_exc).__name__}: {last_exc}",
                            "handled": True,
                            "attempts": attempt,
                            "traceback": wf_engine._trimmed_traceback(last_exc),
                        }
                    )
                    return None
                raise last_exc
            delay_s = _delay_seconds(attempt)
            run_ctx["events"].append(
                {
                    "ts": time.time(),
                    "run_id": run_ctx.get("run_id"),
                    "node_id": nid,
                    "kind": "node_retry",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "error": f"{type(last_exc).__name__}: {last_exc}",
                    "backoff_ms": int(delay_s * 1000),
                }
            )
            await asyncio.sleep(delay_s)
            attempt += 1

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
