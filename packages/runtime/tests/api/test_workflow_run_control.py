"""Round A2 API: run↔session binding, pause/human, decide(resume), cancel."""

from __future__ import annotations

import pytest

from ginno_runtime import server
from ginno_runtime.testing.fake_model import script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _human_wf():
    return {
        "name": "Ctrl",
        "dsl": {
            "entry": "h",
            "nodes": [
                {"id": "h", "type": "human", "question": "approve?"},
                {"id": "s", "type": "step", "agent": "dev", "goal": "after"},
            ],
            "edges": [{"from": "h", "to": "s"}],
        },
    }


def _patch_model(monkeypatch):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: __import__(
            "ginno_runtime.testing.fake_model", fromlist=["ScriptedChatModel"]
        ).ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"z": 1}')]),
    )


def test_create_run_stores_session_binding(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    r = client.post(
        "/api/workflow_runs",
        json={"workflow_id": wf["id"], "session_id": "sess-1", "present_in_session_id": "sess-1"},
    )
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["session_id"] == "sess-1"
    assert run["present_in_session_id"] == "sess-1"


def test_human_run_pauses_then_decide_resumes_to_done(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw

    # resume is rejected unless paused? (it IS paused) -> decide continues it
    d = client.post(f"/api/workflow_runs/{run_id}/decide", json={"decision": "continue"})
    assert d.status_code == 200
    aw2 = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    kinds = [e["kind"] for e in evs]
    assert "interrupt" in kinds and "resume" in kinds and "done" in kinds


def test_resume_non_paused_run_is_409(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(
        {"name": "np", "dsl": {"entry": "s", "nodes": [{"id": "s", "type": "step", "agent": "dev", "goal": "g"}], "edges": []}}
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # completes -> done
    assert client.post(f"/api/workflow_runs/{run_id}/resume", json={}).status_code == 409


def test_cancel_paused_run_marks_cancelled(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # pauses at human
    c = client.post(f"/api/workflow_runs/{run_id}/cancel")
    assert c.status_code == 200
    assert store.get_run(run_id)["status"] == "cancelled"
