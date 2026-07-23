"""Tests for the versioned workflow store + HTTP endpoints (P1).

Covers: create from legacy steps (DSL+steps projection+version), versioned
update, list/get/diff/rollback, legacy single-file migration, run version
pinning, event log append/read, and that the existing workflow_* tools still
see a `steps` view after the refactor.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths
from ginno_runtime.workflows import dsl, events, store

pytestmark = pytest.mark.unit


def _write_legacy(wf_id: str, payload: dict) -> None:
    paths.workflows_dir().mkdir(parents=True, exist_ok=True)
    (paths.workflows_dir() / f"{wf_id}.json").write_text(json.dumps(payload))


# ---- store: create / update / versions ----
def test_create_from_legacy_steps_produces_dsl_and_steps_view(isolated_home):
    wf = store.create_def(
        {"name": "W", "description": "d", "steps": [{"title": "a"}, {"title": "b"}]}
    )
    assert wf["version"] == 1
    assert wf["current"] == 1
    assert isinstance(wf["dsl"], dict)
    assert wf["dsl"]["entry"] == "s1"
    assert [s["title"] for s in wf["steps"]] == ["a", "b"]  # legacy view intact


def test_update_dsl_creates_new_version_keeps_history(isolated_home):
    wf = store.create_def({"name": "W", "steps": [{"title": "a"}]})
    wid = wf["id"]
    new_dsl = dict(wf["dsl"])
    new_dsl["nodes"] = new_dsl["nodes"] + [
        {"id": "s2", "type": "step", "goal": "added"}
    ]
    new_dsl["edges"] = new_dsl["edges"] + [{"from": "s1", "to": "s2"}]
    wf2 = store.update_def(wid, {"dsl": new_dsl})
    assert wf2["version"] == 2
    assert store.list_versions(wid) == [
        {"version": 1, "current": False},
        {"version": 2, "current": True},
    ]
    assert len(store.get_version(wid, 1)["nodes"]) == 1
    assert len(store.get_version(wid, 2)["nodes"]) == 2


def test_update_name_only_does_not_bump_version(isolated_home):
    wf = store.create_def({"name": "W", "steps": [{"title": "a"}]})
    wf2 = store.update_def(wf["id"], {"name": "Renamed"})
    assert wf2["version"] == 1
    assert wf2["name"] == "Renamed"


def test_rollback_creates_new_version_from_old_snapshot(isolated_home):
    wf = store.create_def({"name": "W", "steps": [{"title": "a"}]})
    wid = wf["id"]
    d2 = dict(wf["dsl"])
    d2["nodes"] = d2["nodes"] + [{"id": "s2", "type": "step", "goal": "x"}]
    d2["edges"] = d2["edges"] + [{"from": "s1", "to": "s2"}]
    store.update_def(wid, {"dsl": d2})
    wf3 = store.rollback(wid, 1)
    assert wf3["version"] == 3
    assert len(wf3["dsl"]["nodes"]) == 1  # copy of v1
    assert [v["version"] for v in store.list_versions(wid)] == [1, 2, 3]


def test_create_rejects_invalid_dsl(isolated_home):
    with pytest.raises(ValueError):
        store.create_def({"name": "bad", "dsl": {"entry": "nope", "nodes": []}})


# ---- legacy migration ----
def test_legacy_single_file_migrates_on_get(isolated_home):
    _write_legacy(
        "old",
        {
            "id": "old",
            "name": "Old",
            "description": "legacy",
            "steps": [{"id": "s1", "title": "only"}],
        },
    )
    wf = store.get_def("old")
    assert wf is not None
    assert wf["version"] == 1
    assert wf["steps"][0]["title"] == "only"
    assert wf["dsl"]["entry"] == "s1"
    # legacy file removed, new layout present
    assert not (paths.workflows_dir() / "old.json").exists()
    assert (paths.workflows_dir() / "old" / "meta.json").exists()
    # listed exactly once
    assert [w["id"] for w in store.list_defs()] == ["old"]


# ---- runs pin version + project steps ----
def test_create_run_pins_dsl_version_and_projects_steps(isolated_home):
    wf = store.create_def({"name": "W", "steps": [{"title": "a"}, {"title": "b"}]})
    run = store.create_run(wf, session_id="s1")
    assert run["dsl_version"] == 1
    assert [s["id"] for s in run["steps"]] == ["s1", "s2"]
    assert run["session_id"] == "s1"


# ---- events ----
def test_events_append_and_filter(isolated_home):
    events.append_event("r1", "node_enter", node_id="a")
    events.append_event("r1", "tool_call", node_id="a", name="bash")
    events.append_event("r1", "node_enter", node_id="b")
    assert len(events.read_events("r1")) == 3
    assert len(events.read_events("r1", node_id="a")) == 2
    assert len(events.read_events("r1", kind="tool_call")) == 1
    assert events.read_events("r1", node_id="a", kind="tool_call")[0]["name"] == "bash"


# ---- existing tools still see steps after refactor ----
def test_workflow_list_tool_sees_steps_after_refactor(isolated_home):
    from ginno_runtime.tools.workflow_tools import workflow_list

    store.ensure_seeded()
    out = workflow_list.invoke({})
    assert "PR Triage" in out
    assert "3 steps" in out


# ---- HTTP endpoints ----
def test_get_workflow_endpoint_returns_dsl_and_steps(client):
    r = client.get("/api/workflows/pr-triage")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["workflow"]["version"] == 1
    assert isinstance(body["workflow"]["dsl"], dict)
    assert len(body["workflow"]["steps"]) == 3


def test_get_workflow_endpoint_404(client):
    assert client.get("/api/workflows/does-not-exist").status_code == 404


def test_versions_diff_and_rollback_endpoints(client):
    wid = "pr-triage"
    wf = client.get(f"/api/workflows/{wid}").json()["workflow"]
    d2 = dict(wf["dsl"])
    d2["nodes"] = d2["nodes"] + [{"id": "s4", "type": "step", "goal": "extra"}]
    d2["edges"] = d2["edges"] + [{"from": "s3", "to": "s4"}]
    # update via store (no PUT-dsl endpoint in P1; version endpoints are read+rollback)
    store.update_def(wid, {"dsl": d2})

    vlist = client.get(f"/api/workflows/{wid}/versions").json()
    assert [v["version"] for v in vlist["versions"]] == [1, 2]

    diff = client.get(f"/api/workflows/{wid}/versions/diff", params={"a": 1, "b": 2}).json()
    assert diff["ok"] is True
    assert "extra" in diff["diff"]

    v1 = client.get(f"/api/workflows/{wid}/versions/1").json()
    assert len(v1["dsl"]["nodes"]) == 3

    rb = client.post(f"/api/workflows/{wid}/rollback", json={"to": 1}).json()
    assert rb["ok"] is True
    assert rb["workflow"]["version"] == 3
    assert len(rb["workflow"]["dsl"]["nodes"]) == 3

    assert client.get(f"/api/workflows/{wid}/versions/99").status_code == 404
    assert (
        client.get(f"/api/workflows/{wid}/versions/diff", params={"a": 1, "b": 99}).status_code
        == 404
    )
