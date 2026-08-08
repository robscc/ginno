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
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: __import__(
            "ginno_runtime.testing.fake_model", fromlist=["ScriptedChatModel"]
        ).ScriptedChatModel(
            scripts=[script(text='a\nWRITE_JSON {"x": 1}'), script(text='b\nWRITE_JSON {"y": 2}')]
        ),
    )

    r = client.post("/api/workflow_runs", json={"workflow_id": wid})
    assert r.status_code == 200
    run_id = r.json()["run"]["id"]

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert aw["run"]["status"] == "done", {"await": aw, "events": evs}
    run_id = run_id  # keep

    kinds = [e["kind"] for e in evs]
    assert kinds.count("node_enter") == 2
    assert "done" in kinds
    written = {k for e in evs if e["kind"] == "context_write" for k in e["keys"]}
    assert written == {"x", "y"}


def test_trigger_run_404_for_unknown_workflow(client):
    assert client.post("/api/workflow_runs", json={"workflow_id": "nope"}).status_code == 404


class _BoomModel:
    """Engine-path failure: the step node's model raises on first invoke."""

    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        raise RuntimeError("kaboom")


def test_failed_run_exposes_error_detail(client, monkeypatch):
    """An engine-level failure must expose structured localization data: the
    failing node id + a trimmed traceback, on the run record AND the events."""
    wf = store.create_def(
        {
            "name": "Boom",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
                "edges": [],
            },
        }
    )
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: _BoomModel())

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    # one-line error semantics unchanged
    assert "kaboom" in (run.get("error") or "")
    # structured companion for localization
    detail = run.get("error_detail") or {}
    assert detail.get("node_id") == "s1"
    assert "kaboom" in (detail.get("traceback") or "")
    assert "Traceback" in (detail.get("traceback") or "")

    # GET /api/workflow_runs/{id} exposes the same field
    got = client.get(f"/api/workflow_runs/{run_id}").json()["run"]
    assert (got.get("error_detail") or {}).get("node_id") == "s1"

    # the persisted error event carries node_id + traceback too
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    err = next(e for e in evs if e["kind"] == "error")
    assert err.get("node_id") == "s1"
    assert "kaboom" in (err.get("traceback") or "")
    # incremental flush kept the failing node's footprint
    assert any(e["kind"] == "node_enter" and e.get("node_id") == "s1" for e in evs)


def test_dep_failure_error_detail_has_traceback_no_node(client, monkeypatch):
    """Driver-level failure (agent fork / model build) has no node — node_id is
    None but a traceback is still captured via _mark_run_failed."""
    wf = store.create_def(
        {
            "name": "Ghost",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "ghost", "goal": "x"}],
                "edges": [],
            },
        }
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    detail = run.get("error_detail") or {}
    assert detail.get("node_id") is None
    assert detail.get("traceback"), "driver failures must still capture a traceback"
    assert "Traceback" in detail["traceback"]
