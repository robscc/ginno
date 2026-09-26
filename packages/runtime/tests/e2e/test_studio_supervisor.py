"""Studio e2e: supervisor gates (方案B 阶段2, design B §8.5).

Drives the REAL engine through the API with the shared ``studio_lib`` builders:
the compiler-injected ``<N>__sup`` checkpoints in human mode (park → /decide),
the decision vocabulary (continue/skip/retry/abort + invalid fallback),
``context_patch`` reach, event ordering at a gate, config validation at CREATE,
and the decision-inbox / run-list integration. Auto mode (P2.5) is covered as
one adjudicated pass-through here; its ladder/budgets live in
``tests/api/test_workflow_supervisor_auto.py``.

Every run forks a FRESH scripted model that replays from index 0 on each
run/resume, so script lists only need one entry per model call per invocation.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import PrivateAttr

import studio_lib as L
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _park(client: TestClient, monkeypatch, doc: dict, texts: list) -> tuple[dict, str]:
    """Create + patch model + run + await the first park. Returns (run, id)."""
    wf = L.make_wf(client, doc)
    L.patch_model(monkeypatch, texts)
    rid = L.run_wf(client, wf["id"])
    return L.await_run(client, rid), rid


def _sup_events(client: TestClient, rid: str) -> list:
    return L.evs(client, rid, kind="supervisor_decision")


def _decisions(client: TestClient, rid: str) -> list:
    return [e.get("decision") for e in _sup_events(client, rid)]


# A gate on the graph's LAST node (continue_to=None) suspends the engine but
# the run JSON lands status="done" instead of "paused": the last real step's
# node_exit recomputes the run "done" (store.update_step, all listed steps
# terminal — the injected gate is not a step) BEFORE the paused transition,
# whose only_from=("running","paused") guard then blocks it. Consequences:
# invisible in the decision inbox and /decide → 409 — the run can never be
# decided. Hit by cases 2/3/5/16; not editable here (no product edits).
# (was _END_GATE_PAUSED_BUG — FIXED: the paused transition now overrides a
# premature step-recompute "done", so an end-of-graph gate stays decidable.)


def _assert_create_rejected(client: TestClient, doc: dict, needle: str) -> None:
    """Invalid supervisor configs must never create a workflow — the endpoint
    answers 400 carrying the first validation error (since the P2 fix; the
    store's ValueError used to escape as an unhandled 500)."""
    r = client.post(f"{L.API}/workflows", json=doc)
    assert r.status_code == 400, r.text
    assert needle in str(r.json().get("detail", "")), f"expected {needle!r} in: {r.text}"


# --------------------------------------------------------------------------- #
# 1–7: placement + parking
# --------------------------------------------------------------------------- #
def test_01_disabled_supervisor_is_a_plain_run(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.dsl_linear(2), ["one", "two"])
    assert run["status"] == "done"
    assert L.steps(run) == {"s1": "done", "s2": "done"}  # plain — no __sup/__extract
    ks = L.kinds(L.evs(client, rid))
    assert "interrupt" not in ks and "supervisor_decision" not in ks


def test_02_every_step_parks_at_the_first_gate(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2)), ["one", "two"])
    assert run["status"] == "paused"
    pi = run["pending_interrupt"]
    assert pi["kind"] == "supervisor" and pi["node_id"] == "s1" and pi["gate"] == "s1__sup"
    # every_step gates s2 as well: continue walks to the second gate, parks,
    # and the second continue finishes the run
    assert L.decide(client, rid, "continue").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "paused"
    assert run2["pending_interrupt"]["gate"] == "s2__sup"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"


def test_03_after_nodes_subset_gates_only_the_listed_node(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s2"]), ["one", "two"])
    # s2 ends the graph → its gate is the end-of-graph gate (see bug note)
    assert run["pending_interrupt"]["node_id"] == "s2"
    assert L.steps(run)["s1"] == "done"  # s1 passed WITHOUT pausing
    assert run["status"] == "paused"  # PRODUCT: lands "done"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    assert _decisions(client, rid) == ["continue"]  # exactly one gate in the run


def test_04_gate_follows_extract(client, monkeypatch):
    doc = L.gated(L.dsl_linear(2, writes_first=True), after=["s1"])
    run, rid = _park(client, monkeypatch, doc, [L.wj({"items": ["a", "b"]}), "two"])
    assert run["status"] == "paused"
    pi = run["pending_interrupt"]
    assert pi["kind"] == "supervisor" and pi["node_id"] == "s1" and pi["gate"] == "s1__sup"
    assert L.decide(client, rid, "continue").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "done"
    st = L.steps(run2)
    assert st.get("s1__extract") == "done"  # the gate rode on the extract step
    assert st.get("s2") == "done"


def test_05_loop_body_never_gated(client, monkeypatch):
    """lp (loop) and b (body) get no gate; the first park is at ``use`` AFTER
    the loop completed every iteration. ``use`` ends the graph → its gate is
    the end-of-graph gate (see bug note at the top)."""
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_loop()), ["a", "b", "c", "use"])
    assert run["pending_interrupt"]["node_id"] == "use"
    events = L.evs(client, rid)
    first_intr = next(i for i, e in enumerate(events) if e["kind"] == "interrupt")
    iters_before = [
        e for e in events[:first_intr]
        if e["kind"] == "loop_iter" and e.get("node_id") == "lp"
    ]
    assert len(iters_before) == 3  # all 3 items BEFORE any interrupt
    assert run["status"] == "paused"  # PRODUCT: lands "done"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    assert _decisions(client, rid) == ["continue"]  # only the "use" gate fired


def test_06_branch_never_gated(client, monkeypatch):
    """The branch node routes structurally; go_a/go_b ARE gateable — so after
    s1's gate the run parks at go_a's, never at ``gate``."""
    doc = L.gated(L.dsl_branch())
    run, rid = _park(client, monkeypatch, doc, [L.wj({"flag": "go"}), "ra", "rb"])
    assert run["pending_interrupt"]["node_id"] == "s1"
    assert L.decide(client, rid, "continue").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "paused"
    assert run2["pending_interrupt"]["node_id"] == "go_a"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    # no supervisor event ever names the branch node
    sup_ev = L.evs(client, rid)
    assert not any(e.get("node_id") == "gate" and e["kind"] in ("interrupt", "supervisor_decision")
                   for e in sup_ev)


def test_07_human_node_parks_then_its_gate_parks(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_with_human(), after=["h"]), ["one", "two"])
    assert run["status"] == "paused"
    assert run["pending_interrupt"]["kind"] == "human"  # first park: the human node
    assert L.resume(client, rid, {"decision": "continue", "answer": "yes"}).status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "paused"
    pi = run2["pending_interrupt"]  # second park: h's supervisor gate
    assert pi["kind"] == "supervisor" and pi["node_id"] == "h" and pi["gate"] == "h__sup"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"


# --------------------------------------------------------------------------- #
# 8–16: the decision vocabulary
# --------------------------------------------------------------------------- #
def test_08_decide_continue(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    dec = _sup_events(client, rid)[-1]
    assert dec["decision"] == "continue" and dec["mode"] == "human"
    assert dec["node_id"] == "s1" and dec["gate"] == "s1__sup"


def test_09_decide_skip_records_and_reroutes(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert L.decide(client, rid, "skip").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "done"
    assert L.steps(run2)["s2"] == "done"  # the suffix still ran
    assert _decisions(client, rid) == ["skip"]


def test_10_decide_retry_reexecutes_and_refires(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert L.decide(client, rid, "retry").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "paused"  # the gate fired AGAIN
    assert run2["pending_interrupt"]["kind"] == "supervisor"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    enters_s1 = [e for e in L.evs(client, rid, kind="node_enter") if e.get("node_id") == "s1"]
    assert len(enters_s1) == 2  # the gated node really re-ran
    assert _decisions(client, rid) == ["retry", "continue"]


def test_11_decide_abort_ends_with_pending_suffix(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "never"])
    assert L.decide(client, rid, "abort").status_code == 200
    run2 = L.await_run(client, rid)
    assert run2["status"] == "done"  # routed to END, not failed
    assert L.steps(run2)["s2"] == "pending"
    assert _decisions(client, rid) == ["abort"]


def test_12_invalid_decision_falls_back_to_continue(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert L.decide(client, rid, "yolo").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    assert _decisions(client, rid) == ["continue"]  # recorded as the fallback


def test_13_context_patch_reaches_downstream(client, monkeypatch):
    prompts: list = []

    class Recording(ScriptedChatModel):
        """ScriptedChatModel recording prompts (shared list across the fresh
        per-run forks; pydantic → the instance list must be a PrivateAttr)."""

        _seen: list = PrivateAttr(default_factory=list)

        def __init__(self):
            super().__init__(scripts=[script(text=L.wj({"tag": "zzz"})), script(text="ok")])
            self._seen = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            chunk = "\n".join(str(getattr(m, "content", "")) for m in messages)
            self._seen.append(chunk)
            prompts.append(chunk)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: Recording()
    )
    wf = L.make_wf(client, L.gated(L.dsl_template(), after=["s1"]))
    rid = L.run_wf(client, wf["id"])
    L.await_run(client, rid)
    assert L.decide(client, rid, "continue", patch={"tag": "zzz"}).status_code == 200
    run = L.await_run(client, rid)
    assert run["status"] == "done"
    dec = _sup_events(client, rid)[-1]
    assert dec["context_patch"] == ["tag"]
    assert any("value=zzz" in p for p in prompts)  # s2 rendered the patched value


def test_14_event_order_at_a_gate(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert run["status"] == "paused"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    seq = [
        (e["kind"], e.get("nature"))
        for e in L.evs(client, rid)
        if e["kind"] in ("interrupt", "resume", "supervisor_decision")
    ]
    # Relative order only: langgraph replays the gate node from its top on
    # resume, so the interrupt event legitimately appears twice.
    assert seq[0] == ("interrupt", "supervisor")
    assert ("resume", "supervisor") in seq
    assert seq.index(("resume", "supervisor")) > 0
    assert seq[-1] == ("supervisor_decision", None)
    assert seq[-2] == ("resume", "supervisor")


def test_15_pending_interrupt_cleared_after_done(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert run["pending_interrupt"]["kind"] == "supervisor"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    assert L.get_run(client, rid).get("pending_interrupt") is None


def test_16_two_sequential_gates(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2)), ["one", "two"])
    assert run["pending_interrupt"]["gate"] == "s1__sup"
    assert L.decide(client, rid, "continue").status_code == 200
    run2 = L.await_run(client, rid)
    # PRODUCT: lands "done" instead of pausing at the end-of-graph gate
    assert run2["status"] == "paused" and run2["pending_interrupt"]["gate"] == "s2__sup"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    assert _decisions(client, rid) == ["continue", "continue"]


# --------------------------------------------------------------------------- #
# 17: auto mode (P2.5: LLM adjudicator, decisions applied — ladder details in
# tests/api/test_workflow_supervisor_auto.py)
# --------------------------------------------------------------------------- #
def test_17_auto_mode_adjudicates_and_applies(client, monkeypatch):
    async def _continue(**kwargs):
        return {
            "decision": "continue",
            "confidence": 0.95,
            "reason": "输出正常",
            "context_patch": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr("ginno_runtime.workflows.supervisor_runtime.adjudicate", _continue)
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), mode="auto"), ["one", "two"])
    assert run["status"] == "done"  # adjudicated continue — never paused
    dec = L.evs(client, rid, kind="sup_decision")
    assert len(dec) == 2  # one per gate
    for d in dec:
        assert d["mode"] == "auto" and d["decision"] == "continue"
        assert d["confidence"] == 0.95 and d["reason"] == "输出正常"
        assert d["budget"]["interventions"] == 0
    assert [d["budget"]["tokens"] for d in dec] == [15, 30]  # usage accumulates, rides events
    ev = L.evs(client, rid, kind="sup_eval")
    assert len(ev) == 2
    assert all(e["verdict"] == "ok" and e["confidence_min"] == 0.7 for e in ev)
    # the auto-PENDING pass-through placeholder is gone
    assert L.evs(client, rid, kind="supervisor_decision") == []


# --------------------------------------------------------------------------- #
# 18–20: config validation at CREATE
# --------------------------------------------------------------------------- #
def test_18_invalid_budget_or_confidence_rejected(client, monkeypatch):
    _assert_create_rejected(client, L.gated(L.dsl_linear(2), retry_limit=0), "retry_limit")
    _assert_create_rejected(client, L.gated(L.dsl_linear(2), confidence_min=1.5), "confidence_min")


def test_19_enabled_without_mode_rejected(client):
    doc = L.dsl_linear(2)
    doc["dsl"]["supervisor"] = {"enabled": True}  # mode missing
    _assert_create_rejected(client, doc, "mode")


def test_20_reserved_gate_suffix_rejected(client):
    doc = {
        "name": "R",
        "dsl": {
            "entry": "x__sup",
            "nodes": [{"id": "x__sup", "type": "llm", "prompt": "a"}],
            "edges": [],
        },
    }
    _assert_create_rejected(client, doc, "__sup")


# --------------------------------------------------------------------------- #
# 21–24: control-plane + inbox integration
# --------------------------------------------------------------------------- #
def test_21_pause_while_parked_conflicts(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert run["status"] == "paused"
    r = client.post(f"{L.API}/workflow_runs/{rid}/pause")
    assert r.status_code == 409
    assert L.get_run(client, rid)["status"] == "paused"  # unchanged


def test_22_delete_parked_run(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    r = client.delete(f"{L.API}/workflow_runs/{rid}")
    assert r.status_code == 200 and r.json().get("ok")
    assert L.get_run(client, rid) is None
    inbox = client.get(f"{L.API}/workflow_runs?supervisor_pending=true").json()
    assert not any(x["id"] == rid for x in inbox)


def test_23_decision_inbox_lists_and_releases(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    inbox = client.get(f"{L.API}/workflow_runs?supervisor_pending=true").json()
    mine = [x for x in inbox if x["id"] == rid]
    assert mine and mine[0]["pending_interrupt"]["kind"] == "supervisor"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    inbox2 = client.get(f"{L.API}/workflow_runs?supervisor_pending=true").json()
    assert not any(x["id"] == rid for x in inbox2)


def test_24_retry_budget_holds_across_two_retries(client, monkeypatch):
    run, rid = _park(client, monkeypatch, L.gated(L.dsl_linear(2), after=["s1"]), ["one", "two"])
    assert run["pending_interrupt"]["kind"] == "supervisor"
    for _ in range(2):  # retry → s1 re-runs → gate fires again
        assert L.decide(client, rid, "retry").status_code == 200
        mid = L.await_run(client, rid)
        assert mid["status"] == "paused" and mid["pending_interrupt"]["kind"] == "supervisor"
    assert L.decide(client, rid, "continue").status_code == 200
    assert L.await_run(client, rid)["status"] == "done"
    enters_s1 = [e for e in L.evs(client, rid, kind="node_enter") if e.get("node_id") == "s1"]
    assert len(enters_s1) == 3  # 1 initial + 2 retries
    assert _decisions(client, rid) == ["retry", "retry", "continue"]
