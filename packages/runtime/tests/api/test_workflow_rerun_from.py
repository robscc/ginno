"""rerun_from(node_id) (design B 屏2「从节点重跑」): fork a new run that
re-executes from an arbitrary scheduled node, skipping the completed prefix,
compiled against the run's PINN dsl_version."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _three_node_dsl():
    return {
        "name": "RerunFrom",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "s2", "type": "llm", "prompt": "two"},
                {"id": "s3", "type": "llm", "prompt": "three"},
            ],
            "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"}],
        },
    }


def _model(monkeypatch, texts):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text=t) for t in texts]),
    )


def test_rerun_from_middle_node_skips_prefix(client, monkeypatch):
    wf = store.create_def(_three_node_dsl())
    _model(monkeypatch, ["one", "two", "three"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done"

    # fork from s2: only s2+s3 re-execute (fresh model instance, 2 scripts)
    _model(monkeypatch, ["two-again", "three-again"])
    r = client.post(f"/api/workflow_runs/{rid}/rerun_from", json={"node_id": "s2"})
    assert r.status_code == 200, r.text
    new = r.json()["run"]
    aw2 = client.post(f"/api/workflow_runs/{new['id']}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    # prefix status carried over from the source run
    steps = {s["id"]: s["status"] for s in aw2["run"]["steps"]}
    assert steps == {"s1": "done", "s2": "done", "s3": "done"}
    # the fork really skipped the prefix: no node_enter for s1
    evs = client.get(f"/api/workflow_runs/{new['id']}/events").json()["events"]
    entered = [e["node_id"] for e in evs if e["kind"] == "node_enter"]
    assert entered == ["s2", "s3"]
    # provenance both ways
    assert aw2["run"]["retried_from"] == rid
    src = client.get(f"/api/workflow_runs/{rid}").json()["run"]
    assert src["retry_run_id"] == new["id"]


def test_rerun_from_pins_the_runs_dsl_version(client, monkeypatch):
    wf = store.create_def(_three_node_dsl())
    _model(monkeypatch, ["one", "two", "three"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    assert client.post(f"/api/workflow_runs/{rid}/_await").json()["run"]["status"] == "done"

    # v2 renames a node — the fork must still compile the RUN's v1 snapshot
    v2 = {
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one-v2"},
                {"id": "renamed", "type": "llm", "prompt": "two-v2"},
                {"id": "s3", "type": "llm", "prompt": "three-v2"},
            ],
            "edges": [{"from": "s1", "to": "renamed"}, {"from": "renamed", "to": "s3"}],
        }
    }
    assert client.put(f"/api/workflows/{wf['id']}", json=v2).json()["workflow"]["version"] == 2

    _model(monkeypatch, ["two-again", "three-again"])
    r = client.post(f"/api/workflow_runs/{rid}/rerun_from", json={"node_id": "s2"})
    assert r.status_code == 200, r.text
    new = r.json()["run"]
    assert new["dsl_version"] == 1  # pinned, not "current"
    aw2 = client.post(f"/api/workflow_runs/{new['id']}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    # s2 only exists in v1 — proof the fork executed the pinned snapshot
    entered = [
        e["node_id"]
        for e in client.get(f"/api/workflow_runs/{new['id']}/events").json()["events"]
        if e["kind"] == "node_enter"
    ]
    assert entered == ["s2", "s3"]


def test_rerun_from_rejects(client, monkeypatch):
    wf = store.create_def(_three_node_dsl())
    _model(monkeypatch, ["one", "two", "three"])
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    assert client.post(f"/api/workflow_runs/{rid}/_await").json()["run"]["status"] == "done"

    # no node_id
    assert client.post(f"/api/workflow_runs/{rid}/rerun_from", json={}).status_code == 400
    # node not in the run's pinned DSL
    r = client.post(f"/api/workflow_runs/{rid}/rerun_from", json={"node_id": "nope"})
    assert r.status_code == 409
    # unknown run
    assert client.post("/api/workflow_runs/zzz/rerun_from", json={"node_id": "s1"}).status_code == 404
