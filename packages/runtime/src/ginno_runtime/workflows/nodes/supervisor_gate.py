"""Synthetic supervisor checkpoint gate (design B §8.5).

The compiler injects a ``<N>__sup`` node after every gated node when
``dsl.supervisor.enabled`` — users never write these (the ``__sup`` suffix is
reserved by validate_dsl, like ``__extract``). The stored DSL stays clean; the
injection is re-derived per compile.

human mode (P2) suspends through the SAME interrupt/checkpoint mechanics as
HumanNode and the manual pause, so paused/resume/pending_interrupt all work
without new plumbing. The decision vocabulary:

    continue  proceed to the gated node's successor (default)
    skip      same routing as continue — the gated node ALREADY ran; the skip
              is recorded as a decision event (the observer shows it)
    retry     route BACK to the gated node (it re-executes)
    abort     route to END; the run finishes "done" with a warning count

``context_patch`` may ride along with any decision and merges into context.

auto mode (P2.5/阶段3) adjudicates through an LLM judge
(:mod:`ginno_runtime.workflows.supervisor_runtime`) and applies the decision
directly, subject to the fallback ladder — first hit wins:

    1. judge raised                  → sup_fallback "judge-error"
    2. confidence < confidence_min   → sup_fallback "low-confidence"
    3. applied retries >= retry_limit
       AND decision is retry         → sup_fallback "retry-limit"
    4. interventions >= max_interventions
       AND decision is non-continue  → sup_fallback "interventions-exceeded"
    5. judge token spend > token_budget → sup_fallback "token-budget"

A fallback parks the gate EXACTLY like human mode (same interrupt/checkpoint
mechanics, ``pending_interrupt.kind`` stays "supervisor") with the fallback
reason and the adjudicator's would-be suggestion riding the interrupt payload
— the human card shows "auto 建议：…" and /decide works untouched.

Known replay cost (mirrors the accepted human-mode replay behavior where the
interrupt event appears twice, see studio e2e test_14): on resume, langgraph
re-executes the gate from its top, so the pre-interrupt side effects — one
judge call plus the sup_eval/sup_fallback events — re-fire before
``interrupt()`` returns the human's decision. Budget state
(``run_ctx["supervisor_state"]``) is likewise per engine invocation and resets
on resume.

``on_error`` checkpoints are NOT wired here yet (needs engine error-path
integration — P2.5 follow-up).
"""

from __future__ import annotations

import time

from .base import BaseNode
from .registry import register_node

_DECISIONS = ("continue", "skip", "retry", "abort")

# Fallback ladder defaults (design B §8.5) — used when the supervisor config
# omits the field (0/false are legitimate values, hence the ``is None`` checks).
_DEFAULTS = {"retry_limit": 2, "max_interventions": 5, "token_budget": 20000, "confidence_min": 0.7}

# Chinese lead-in for the fallback park question — the question text STARTS
# with the reason so the human card shows why auto escalated.
_FALLBACK_LABELS = {
    "judge-error": "auto 裁判不可用",
    "low-confidence": "auto 置信度不足",
    "retry-limit": "auto 重试次数达上限",
    "interventions-exceeded": "auto 干预次数达上限",
    "token-budget": "auto 裁判 token 超预算",
}


@register_node
class SupervisorGateNode(BaseNode):
    type = "supervisor_gate"

    @classmethod
    def validate_params(cls, node: dict) -> list[str]:
        return []  # compiler-shaped; users cannot declare one (reserved suffix)

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        from langgraph.types import interrupt

        run_ctx = cctx["run_ctx"]
        gate_id = node["id"]
        src = node.get("source_node") or gate_id
        sup = cctx.get("supervisor") or {}
        question = (
            sup.get("prompt")
            or node.get("question")
            or f"「{src}」执行完毕，是否继续？"
        )
        mode = sup.get("mode") or "human"

        def emit(ev: dict) -> None:
            ev.setdefault("ts", time.time())
            ev["run_id"] = run_ctx.get("run_id")
            ev["node_id"] = src
            ev["gate"] = gate_id
            run_ctx["events"].append(ev)

        if mode != "human":
            auto = await SupervisorGateNode._auto_decide(
                emit, interrupt, node, cctx, state, run_ctx, sup, src
            )
            if auto is not None:
                return auto  # decision applied; the conditional edge routes it
            # A fallback parked the gate; _auto_decide already emitted the
            # sup_fallback + interrupt and pulled the human's resume value off
            # run_ctx — process it through the SAME decision handling as human
            # mode so /decide, inbox and audit events stay identical.
            value = run_ctx.pop(f"__sup_fallback_value_{gate_id}", None)
        else:
            emit({"kind": "interrupt", "nature": "supervisor", "question": question})
            value = interrupt({"kind": "supervisor", "node": src, "question": question})
            emit({"kind": "resume", "nature": "supervisor"})

        decision = value.get("decision") if isinstance(value, dict) else None
        if decision not in _DECISIONS:
            decision = "continue"
        patch = (
            value.get("context_patch")
            if isinstance(value, dict) and isinstance(value.get("context_patch"), dict)
            else None
        )
        emit({
            "kind": "supervisor_decision",
            "decision": decision,
            "mode": "human",
            "context_patch": sorted(patch.keys()) if patch else None,
        })

        update: dict = {"events": [], "__output__": {"decision": decision}}
        if patch:
            ctx = dict(state.get("context") or {})
            update["context"] = {**ctx, **patch}
        return update

    # ------------------------------------------------------------------ #
    # auto mode (P2.5): judge + fallback ladder
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _auto_decide(emit, interrupt, node, cctx, state, run_ctx, sup, src) -> dict | None:
        """Judge the gated node's output and either apply the decision (returns
        the state update) or park on a human via the fallback ladder (returns
        None; the human's resume value is stashed on run_ctx for the caller)."""
        gate_id = node["id"]
        st = run_ctx.setdefault(
            "supervisor_state", {"interventions": 0, "tokens": 0, "retries": {}}
        )
        conf_min = sup.get("confidence_min")
        conf_min = _DEFAULTS["confidence_min"] if conf_min is None else float(conf_min)
        retry_limit = sup.get("retry_limit")
        retry_limit = _DEFAULTS["retry_limit"] if retry_limit is None else int(retry_limit)
        max_intv = sup.get("max_interventions")
        max_intv = _DEFAULTS["max_interventions"] if max_intv is None else int(max_intv)
        token_budget = sup.get("token_budget")
        token_budget = _DEFAULTS["token_budget"] if token_budget is None else float(token_budget)

        def budget() -> dict:
            return {
                "interventions": st["interventions"],
                "max_interventions": max_intv,
                "tokens": st["tokens"],
                "token_budget": token_budget,
            }

        # The judge is called THROUGH the module so tests can monkeypatch
        # ginno_runtime.workflows.supervisor_runtime.adjudicate.
        from .. import supervisor_runtime as sup_rt

        verdict: dict | None = None
        judge_err = ""
        try:
            outs = state.get("outputs") or {}
            node_output = outs.get(f"{src}__extract", outs.get(src))
            ntype = next(
                (
                    n.get("type")
                    for n in (cctx.get("dsl") or {}).get("nodes") or []
                    if isinstance(n, dict) and n.get("id") == src
                ),
                None,
            )
            verdict = await sup_rt.adjudicate(
                node_id=src,
                node_type=ntype,
                node_output=node_output,
                context=dict(state.get("context") or {}),
                recent_events=(run_ctx.get("events") or [])[-8:],
                policy=sup.get("policy") or "",
                model=sup_rt._judge_model(sup, cctx),
                run_ctx=run_ctx,
            )
        except Exception as exc:  # noqa: BLE001 — ANY judge failure escalates
            verdict = None
            judge_err = f"{type(exc).__name__}: {exc}"

        reason: str | None = None
        detail = ""
        if verdict is None:
            reason, detail = "judge-error", f"裁判模型调用失败：{judge_err[:200]}"
        else:
            conf = float(verdict["confidence"])
            decision = verdict["decision"]
            usage = verdict.get("usage") or {}
            st["tokens"] += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            # One sup_eval per judged call, BEFORE the ladder / sup_decision.
            emit({
                "kind": "sup_eval",
                "confidence": conf,
                "confidence_min": conf_min,
                "verdict": "ok" if conf >= conf_min else "low-confidence",
            })
            if conf < conf_min:
                reason, detail = "low-confidence", f"置信度 {conf:g} < 阈值 {conf_min:g}（节点「{src}」）"
            elif decision == "retry" and st["retries"].get(src, 0) >= retry_limit:
                reason = "retry-limit"
                detail = f"节点「{src}」已重试 {st['retries'].get(src, 0)} 次，达上限 {retry_limit}"
            elif decision != "continue" and st["interventions"] >= max_intv:
                reason = "interventions-exceeded"
                detail = f"已干预 {st['interventions']} 次，达上限 {max_intv}"
            elif st["tokens"] > token_budget:
                reason = "token-budget"
                detail = f"裁判累计消耗 {st['tokens']} tokens，超预算 {token_budget:g}"

        if reason is not None:
            sug = None
            if verdict is not None:
                sug = {
                    "decision": verdict.get("decision"),
                    "confidence": verdict.get("confidence"),
                    "reason": verdict.get("reason") or "",
                }
            q = f"{_FALLBACK_LABELS[reason]}({detail})，转人工"
            if sug:
                q += f"；auto 建议：{sug['decision']}（{sug['reason']}）"
            emit({"kind": "sup_fallback", "reason": reason, "detail": detail})
            emit({
                "kind": "interrupt",
                "nature": "supervisor",
                "question": q,
                "fallback_reason": reason,
                "auto_suggestion": sug,
            })
            value = interrupt({
                "kind": "supervisor",
                "node": src,
                "question": q,
                "fallback_reason": reason,
                "auto_suggestion": sug,
            })
            emit({"kind": "resume", "nature": "supervisor"})
            run_ctx[f"__sup_fallback_value_{gate_id}"] = value
            return None

        # Ladder passed — apply the decision.
        decision = verdict["decision"]
        patch = verdict.get("context_patch")
        if decision == "retry":
            st["retries"][src] = st["retries"].get(src, 0) + 1
        if decision != "continue":
            st["interventions"] += 1
        emit({
            "kind": "sup_decision",
            "decision": decision,
            "mode": "auto",
            "confidence": float(verdict["confidence"]),
            "reason": verdict.get("reason") or "",
            "context_patch": sorted(patch.keys()) if isinstance(patch, dict) and patch else None,
            "budget": budget(),
        })
        update: dict = {"events": [], "__output__": {"decision": decision}}
        if isinstance(patch, dict) and patch:
            update["context"] = {**(state.get("context") or {}), **patch}
        return update

    @classmethod
    def add_edges(cls, g, node, d) -> None:
        from langgraph.graph import END

        nid = node["id"]
        src = node.get("source_node") or nid
        # Injected at compile time (see compiler._inject_supervisor_gates):
        # the gated node's original successor, or None when it ended the graph.
        nxt = node.get("continue_to")

        def route(state, config=None) -> str:
            outs = state.get("outputs") or {}
            decision = (outs.get(nid) or {}).get("decision") or "continue"
            if decision == "retry":
                return src
            if decision == "abort":
                return END
            return nxt or END  # continue / skip

        targets = {src, END}
        if nxt:
            targets.add(nxt)
        g.add_conditional_edges(nid, route, {t: t for t in targets} | {END: END})
