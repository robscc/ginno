"""Stability plan P2 API tests: a run whose step retries through a transient
failure, and a run whose step soft-fails (on_error=continue) while the run
finishes "done" with a warnings count."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def test_retry_run_succeeds_after_transient_failure(client, monkeypatch):
    wf = store.create_def(
        {
            "name": "RetryRun",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "flaky",
                     "retry": {"max_attempts": 3, "backoff": "fixed", "backoff_ms": 0}},
                ],
                "edges": [],
            },
        }
    )
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(
            scripts=[script_raise(RuntimeError("provider blip")), script(text="ok")]
        ),
    )
    r = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]})
    run_id = r.json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert aw["run"]["status"] == "done", {"await": aw, "events": evs}
    kinds = [e["kind"] for e in evs]
    assert "node_retry" in kinds  # persisted + pushed like any event
    retry_ev = next(e for e in evs if e["kind"] == "node_retry")
    assert retry_ev["node_id"] == "s1" and retry_ev["attempt"] == 1
    assert kinds[-1] == "done"
    # the step lands done after the recovered attempt
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps["s1"] == "done"


def test_on_error_continue_run_finishes_done_with_warning(client, monkeypatch):
    wf = store.create_def(
        {
            "name": "SoftFailRun",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "doomed",
                     "on_error": "continue"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "carry on"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(
            scripts=[script_raise(RuntimeError("soft boom")), script(text="s2 fine")]
        ),
    )
    r = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]})
    run_id = r.json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    run = aw["run"]
    # Run still ends DONE — but with a warnings count and a failed step.
    assert run["status"] == "done", {"await": aw, "events": evs}
    assert run.get("warnings") == 1
    steps = {s["id"]: s["status"] for s in run["steps"]}
    assert steps["s1"] == "failed"
    assert steps["s2"] == "done"
    # the error event is persisted with handled:true (never flipped the run)
    errs = [e for e in evs if e["kind"] == "error"]
    assert len(errs) == 1 and errs[0].get("handled") is True
    assert errs[0]["node_id"] == "s1"
