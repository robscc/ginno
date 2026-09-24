"""GET /api/workflow_runs optional filters (workflow_id / status).

The Studio run list needs (a) runs of one workflow and (b) runs by status.
Covers the store-level filter contract (backward compatible no-arg call,
comma-separated statuses, legacy run JSON missing fields) and the endpoint
pass-through. No LLM is involved: runs are seeded via the store and their
statuses flipped via ``server._set_run_status``."""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths, server
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _step_wf(name: str = "W"):
    return {
        "name": name,
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "do it"}],
            "edges": [],
        },
    }


def _seed_runs():
    """Two workflows with runs in different statuses. Returns (wf_a, wf_b,
    a_running, a_paused, b_done)."""
    paths.ensure_layout()
    wf_a = store.create_def(_step_wf("A"))
    wf_b = store.create_def(_step_wf("B"))
    a_running = store.create_run(wf_a)
    a_paused = store.create_run(wf_a)
    server._set_run_status(a_paused["id"], "paused")
    b_done = store.create_run(wf_b)
    server._set_run_status(b_done["id"], "done")
    return wf_a, wf_b, a_running, a_paused, b_done


def _write_legacy_run(run_id: str, payload: dict) -> None:
    """A run JSON from an older schema (missing workflow_id/status fields)."""
    runs_dir = paths.home() / "workflow_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(json.dumps(payload))


# --- store level ---


def test_list_runs_no_args_unchanged(isolated_home):
    """No filters = today's behaviour: every run, newest first."""
    _, _, a_running, a_paused, b_done = _seed_runs()
    runs = store.list_runs()
    ids = [r["id"] for r in runs]
    assert set(ids) == {a_running["id"], a_paused["id"], b_done["id"]}
    # unchanged order: newest first by `started`
    started = [r.get("started", 0) for r in runs]
    assert started == sorted(started, reverse=True)


def test_list_runs_workflow_id_filter(isolated_home):
    wf_a, _, a_running, a_paused, b_done = _seed_runs()
    ids = {r["id"] for r in store.list_runs(workflow_id=wf_a["id"])}
    assert ids == {a_running["id"], a_paused["id"]}
    assert b_done["id"] not in ids


def test_list_runs_status_single(isolated_home):
    _, _, _, a_paused, _ = _seed_runs()
    ids = [r["id"] for r in store.list_runs(status="paused")]
    assert ids == [a_paused["id"]]


def test_list_runs_status_comma_list_whitespace_tolerant(isolated_home):
    _, _, a_running, a_paused, b_done = _seed_runs()
    ids = {r["id"] for r in store.list_runs(status=" running , paused ,,")}
    assert ids == {a_running["id"], a_paused["id"]}
    assert b_done["id"] not in ids


def test_list_runs_filter_matching_nothing(isolated_home):
    _seed_runs()
    assert store.list_runs(workflow_id="nope") == []
    assert store.list_runs(status="nope") == []


def test_list_runs_legacy_record_missing_fields(isolated_home):
    """A legacy run without workflow_id/status must not crash the filter; it
    simply never matches a filter on the missing field but stays in the
    unfiltered list."""
    wf_a, _, a_running, _, _ = _seed_runs()
    _write_legacy_run("legacy0001", {"id": "legacy0001", "started": 1.0})
    all_ids = {r["id"] for r in store.list_runs()}
    assert "legacy0001" in all_ids
    assert "legacy0001" not in {r["id"] for r in store.list_runs(workflow_id=wf_a["id"])}
    assert "legacy0001" not in {r["id"] for r in store.list_runs(status="running")}
    assert a_running["id"] in {r["id"] for r in store.list_runs(status="running")}


# --- endpoint level ---


def test_endpoint_no_filter_returns_all(client):
    _, _, a_running, a_paused, b_done = _seed_runs()
    r = client.get("/api/workflow_runs")
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()}
    assert ids == {a_running["id"], a_paused["id"], b_done["id"]}


def test_endpoint_workflow_id_filter(client):
    wf_a, _, a_running, a_paused, b_done = _seed_runs()
    r = client.get("/api/workflow_runs", params={"workflow_id": wf_a["id"]})
    assert r.status_code == 200
    assert {row["id"] for row in r.json()} == {a_running["id"], a_paused["id"]}


def test_endpoint_status_comma_list(client):
    _, _, a_running, a_paused, b_done = _seed_runs()
    r = client.get("/api/workflow_runs", params={"status": "running,paused"})
    assert r.status_code == 200
    assert {row["id"] for row in r.json()} == {a_running["id"], a_paused["id"]}
    assert b_done["id"] not in {row["id"] for row in r.json()}


def test_endpoint_combined_filters(client):
    wf_a, _, a_running, _, _ = _seed_runs()
    r = client.get(
        "/api/workflow_runs", params={"workflow_id": wf_a["id"], "status": "running"}
    )
    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == [a_running["id"]]


def test_endpoint_filter_matching_nothing_returns_empty_200(client):
    _seed_runs()
    r = client.get("/api/workflow_runs", params={"workflow_id": "nope"})
    assert r.status_code == 200
    assert r.json() == []


def test_endpoint_legacy_record_does_not_break_filter(client):
    wf_a, _, _, _, _ = _seed_runs()
    _write_legacy_run("legacy0002", {"id": "legacy0002"})
    r = client.get("/api/workflow_runs", params={"workflow_id": wf_a["id"]})
    assert r.status_code == 200
    assert all(row.get("id") != "legacy0002" for row in r.json())
    r_all = client.get("/api/workflow_runs")
    assert r_all.status_code == 200
    assert "legacy0002" in {row["id"] for row in r_all.json()}
