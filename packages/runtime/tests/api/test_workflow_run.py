"""API test for the P2 workflow execution path (trigger + background run + events)."""

from __future__ import annotations

import pytest

from ginno_runtime import server
from ginno_runtime.testing.fake_model import script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def test_trigger_run_executes_and_writes_context(client, monkeypatch):
    # dev agent is seeded by the lifespan; the workflow's step references it.
    wf = store.create_def(
        {
            "name": "RunMe",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "compute"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "finalize"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    wid = wf["id"]
    monkeypatch.setattr(
        server,
        "build_model",
        lambda *a, **k: __import__(
            "ginno_runtime.testing.fake_model", fromlist=["ScriptedChatModel"]
        ).ScriptedChatModel(
            scripts=[script(text='a\nWRITE_JSON {"x": 1}'), script(text='b\nWRITE_JSON {"y": 2}')]
        ),
    )

    r = client.post("/workflow_runs", json={"workflow_id": wid})
    assert r.status_code == 200
    run_id = r.json()["run"]["id"]

    aw = client.post(f"/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/workflow_runs/{run_id}/events").json()["events"]
    assert aw["run"]["status"] == "done", {"await": aw, "events": evs}
    run_id = run_id  # keep

    kinds = [e["kind"] for e in evs]
    assert kinds.count("node_enter") == 2
    assert "done" in kinds
    written = {k for e in evs if e["kind"] == "context_write" for k in e["keys"]}
    assert written == {"x", "y"}


def test_trigger_run_404_for_unknown_workflow(client):
    assert client.post("/workflow_runs", json={"workflow_id": "nope"}).status_code == 404
