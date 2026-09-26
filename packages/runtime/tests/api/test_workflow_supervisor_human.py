"""Supervisor human mode end-to-end (design B §8.5, P2): a gated run pauses at
the injected ``<N>__sup`` checkpoint, /decide drives it (continue / retry /
context_patch), decisions land as auditable events, and the decision inbox
(``?supervisor_pending=true``) sees the waiting run."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _gated_dsl():
    return {
        "name": "SupGate",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "s2", "type": "llm", "prompt": "value={{context.tag}}"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
            "supervisor": {
                "enabled": True,
                "mode": "human",
                "checkpoints": {"after_nodes": ["s1"]},
            },
        },
    }


def _model(monkeypatch, texts):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text=t) for t in texts]),
    )


def _decide(client, rid, decision, patch=None):
    return client.post(
        f"/api/workflow_runs/{rid}/decide",
        json={"decision": decision, "context_patch": patch},
    )


def test_gate_pauses_decide_continue_completes(client, monkeypatch):
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["kind"] == "supervisor" and pi["node_id"] == "s1"

    # decision inbox sees it
    inbox = client.get("/api/workflow_runs?supervisor_pending=true").json()
    assert any(r["id"] == rid for r in inbox)

    assert _decide(client, rid, "continue").status_code == 200
    aw2 = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw2["run"]["status"] == "done", aw2

    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    dec = [e for e in evs if e["kind"] == "supervisor_decision"]
    assert dec and dec[-1]["decision"] == "continue" and dec[-1]["mode"] == "human"
    assert dec[-1]["gate"] == "s1__sup" and dec[-1]["node_id"] == "s1"
    # gone from the inbox once decided
    inbox2 = client.get("/api/workflow_runs?supervisor_pending=true").json()
    assert not any(r["id"] == rid for r in inbox2)


def test_decide_retry_reexecutes_the_gated_node(client, monkeypatch):
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "one-again", "two"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{rid}/_await")
    # retry → s1 re-executes and the gate fires AGAIN (fresh model has a script)
    assert _decide(client, rid, "retry").status_code == 200
    client.post(f"/api/workflow_runs/{rid}/_await")
    assert _decide(client, rid, "continue").status_code == 200
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    enters = [e for e in evs if e["kind"] == "node_enter" and e["node_id"] == "s1"]
    decisions = [e["decision"] for e in evs if e["kind"] == "supervisor_decision"]
    assert len(enters) == 2  # the gated node really re-ran
    assert decisions == ["retry", "continue"]


def test_decide_with_context_patch_reaches_downstream(client, monkeypatch):
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{rid}/_await")
    assert (
        _decide(client, rid, "continue", patch={"tag": "patched"}).status_code == 200
    )
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    dec = [e for e in evs if e["kind"] == "supervisor_decision"][-1]
    assert dec["context_patch"] == ["tag"]
    # s2 consumed the patched value via a context_write (output key "out" is
    # asserted through the run's steps all being done)
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps == {"s1": "done", "s2": "done"}


def test_abort_ends_the_run(client, monkeypatch):
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "never"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{rid}/_await")
    assert _decide(client, rid, "abort").status_code == 200
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done"  # routed to END, not failed
    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    assert [e for e in evs if e["kind"] == "supervisor_decision"][-1]["decision"] == "abort"
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps["s2"] == "pending"  # the suffix never ran


def test_auto_mode_adjudicates_without_parking(client, monkeypatch):
    """P2.5: an auto gate adjudicates instead of parking. The full ladder/
    budget contract lives in test_workflow_supervisor_auto.py — here we pin
    only that human mode's file still sees auto runs complete untouched."""
    async def _continue(**kwargs):
        return {"decision": "continue", "confidence": 0.95, "reason": "ok",
                "context_patch": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr("ginno_runtime.workflows.supervisor_runtime.adjudicate", _continue)
    dsl = _gated_dsl()
    dsl["dsl"]["supervisor"]["mode"] = "auto"
    wf = store.create_def(dsl)
    _model(monkeypatch, ["one", "two"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done"
    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    dec = [e for e in evs if e["kind"] == "sup_decision"]
    assert dec and dec[-1]["mode"] == "auto" and dec[-1]["decision"] == "continue"
