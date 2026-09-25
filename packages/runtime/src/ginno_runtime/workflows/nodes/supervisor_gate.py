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

auto mode (P2.5/Phase 3) is not implemented here — a gate compiled with
mode="auto" currently behaves as continue and says so in a decision event
(labeled ``mode: "auto-pending"``) so runs are never silently unobservable.
"""

from __future__ import annotations

import time

from .base import BaseNode
from .registry import register_node

_DECISIONS = ("continue", "skip", "retry", "abort")


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
        question = (
            (cctx.get("supervisor") or {}).get("prompt")
            or node.get("question")
            or f"「{src}」执行完毕，是否继续？"
        )
        mode = (cctx.get("supervisor") or {}).get("mode") or "human"

        def emit(ev: dict) -> None:
            ev.setdefault("ts", time.time())
            ev["run_id"] = run_ctx.get("run_id")
            ev["node_id"] = src
            ev["gate"] = gate_id
            run_ctx["events"].append(ev)

        if mode != "human":
            # P2.5 lands the auto adjudicator; until then an enabled gate in
            # auto mode passes through, visibly.
            emit({"kind": "supervisor_decision", "decision": "continue", "mode": "auto-pending"})
            return {"events": [], "__output__": {"decision": "continue"}}

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
