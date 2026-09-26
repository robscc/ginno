"""POST /api/workflow_runs/{id}/present (设计B「聊天打开本次运行」): re-bind a
run's presenting session so subsequent run.* events stream into an existing
chat — a run that started in the Studio (or headless) becomes followable."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _dsl():
    return {
        "name": "Present",
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "llm", "prompt": "one"}],
            "edges": [],
        },
    }


def test_present_binds_run_and_pushes_run_bind(client, monkeypatch):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="one")]),
    )
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: ScriptedChatModel(scripts=[])
    )
    wf = store.create_def(_dsl())
    # headless run: created WITHOUT a session (the Studio-trigger shape)
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    sid = client.post("/api/sessions", json={
        "project_slug": "default", "workspace": "/tmp/gw", "agent_id": "dev",
    }).json()["id"]

    with client.websocket_connect(f"/api/ws/sessions/{sid}") as ws:
        r = client.post(f"/api/workflow_runs/{rid}/present", json={"session_id": sid})
        assert r.status_code == 200
        f = ws.receive_json()
        assert f["event"] == "run.bind"
        assert f["run_id"] == rid and f["present_in_session_id"] == sid

    run = client.get(f"/api/workflow_runs/{rid}").json()["run"]
    assert run["present_in_session_id"] == sid
    # the run's empty session_id is backfilled with the presenter
    assert run["session_id"] == sid


def test_present_404_unknown_run(client):
    r = client.post("/api/workflow_runs/zzz/present", json={"session_id": "s1"})
    assert r.status_code == 404


def test_present_400_missing_session_id(client):
    wf = store.create_def(_dsl())
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    r = client.post(f"/api/workflow_runs/{rid}/present", json={})
    assert r.status_code == 400
