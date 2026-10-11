"""Cross-run result cache through the API drivers (odw-comparison P1-①):
a second run replays the prefix, no_cache pins a run live, rerun_from
force-executes its target node, and the settings/env gate kills the whole
mechanism. (retry shares _run_workflow_bg with create — covered via create.)"""

from __future__ import annotations

import pytest

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _chain_wf():
    return {
        "name": "RcChain",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "dev", "goal": "first"},
                {"id": "s2", "type": "step", "agent": "dev", "goal": "second"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        },
    }


class _ModelFactory:
    """build_model patch handing out a fresh scripted model per fork and
    counting actual LLM CALLS across all of them — a fully-replayed run
    still forks a model (deps are built up front) but never calls it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k) -> ScriptedChatModel:
        fac = self

        class _Hooked(ScriptedChatModel):
            def _next(self, messages=None):
                fac.calls += 1
                return super()._next(messages)

        # Plenty of scripts: every node of a live run must get non-empty text
        # (an exhausted script returns "" and would not be recorded).
        return _Hooked(scripts=[script(text="out")] * 8)


def _patch_model(monkeypatch):
    fac = _ModelFactory()
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", fac)
    return fac


def _await(client, run_id):
    return client.post(f"/api/workflow_runs/{run_id}/_await").json()


def _create(client, wf_id, extra=None):
    return client.post(
        "/api/workflow_runs", json={"workflow_id": wf_id, **(extra or {})}
    ).json()["run"]["id"]


def test_second_run_replays_prefix(client, monkeypatch):
    fac = _patch_model(monkeypatch)
    wf = store.create_def(_chain_wf())
    r1 = _create(client, wf["id"])
    aw1 = _await(client, r1)
    assert aw1["run"]["status"] == "done", aw1
    served = fac.calls

    r2 = _create(client, wf["id"])
    aw2 = _await(client, r2)
    assert aw2["run"]["status"] == "done", aw2
    # The second run forked ZERO live models — every step replayed.
    assert fac.calls == served
    events = client.get(f"/api/workflow_runs/{r2}/events").json()["events"]
    cached = {e["node_id"] for e in events if e["kind"] == "node_exit" and e.get("cached")}
    assert cached == {"s1", "s2"}
    # The replayed text + cached marker landed in the step table (the chat run
    # block only sees the run JSON, not events).
    steps = {s["id"]: s for s in aw2["run"]["steps"]}
    assert steps["s1"]["cached"] is True and steps["s1"]["output"] == "out"
    assert steps["s2"]["cached"] is True


def test_create_run_no_cache(client, monkeypatch):
    fac = _patch_model(monkeypatch)
    wf = store.create_def(_chain_wf())
    r1 = _create(client, wf["id"])
    assert _await(client, r1)["run"]["status"] == "done"

    r2 = _create(client, wf["id"], {"no_cache": True})
    aw = _await(client, r2)
    assert aw["run"]["status"] == "done", aw
    assert aw["run"].get("cache_disabled") is True
    events = client.get(f"/api/workflow_runs/{r2}/events").json()["events"]
    assert not [e for e in events if e.get("cached")]
    assert fac.calls == 4  # both runs fully live (2 nodes × 2 runs)


def test_settings_toggle_disables(client, monkeypatch):
    fac = _patch_model(monkeypatch)
    wf = store.create_def(_chain_wf())
    r1 = _create(client, wf["id"])
    assert _await(client, r1)["run"]["status"] == "done"
    served = fac.calls

    monkeypatch.setenv("GINNO_WF_RESULT_CACHE", "0")
    r2 = _create(client, wf["id"])
    aw = _await(client, r2)
    assert aw["run"]["status"] == "done", aw
    assert fac.calls == served + 2  # live re-run despite identical inputs (both nodes)
    events = client.get(f"/api/workflow_runs/{r2}/events").json()["events"]
    assert not [e for e in events if e.get("cached")]


def test_rerun_from_target_forces_live(client, monkeypatch):
    fac = _patch_model(monkeypatch)
    wf = store.create_def(_chain_wf())
    r1 = _create(client, wf["id"])
    assert _await(client, r1)["run"]["status"] == "done"
    served = fac.calls

    # rerun_from s2: the TARGET must execute live (replaying it would make the
    # rerun a no-op) — one fresh fork despite a matching fingerprint.
    r2 = client.post(f"/api/workflow_runs/{r1}/rerun_from", json={"node_id": "s2"}).json()["run"]["id"]
    aw = _await(client, r2)
    assert aw["run"]["status"] == "done", aw
    assert fac.calls == served + 1
    events = client.get(f"/api/workflow_runs/{r2}/events").json()["events"]
    cached = {e["node_id"] for e in events if e["kind"] == "node_exit" and e.get("cached")}
    assert "s2" not in cached  # target ran live
    steps = {s["id"]: s for s in aw["run"]["steps"]}
    assert steps["s2"].get("cached") is not True

    # The force-live execution refreshed s2's entry — a later plain run
    # replays BOTH steps again.
    r3 = _create(client, wf["id"])
    aw3 = _await(client, r3)
    assert aw3["run"]["status"] == "done", aw3
    assert fac.calls == served + 1
    steps3 = {s["id"]: s for s in aw3["run"]["steps"]}
    assert steps3["s1"]["cached"] is True and steps3["s2"]["cached"] is True


def test_delete_workflow_drops_cache(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_chain_wf())
    r1 = _create(client, wf["id"])
    assert _await(client, r1)["run"]["status"] == "done"
    idx = paths.workflow_cache_dir() / f"{wf['id']}.json"
    assert idx.exists()

    ok = client.delete(f"/api/workflows/{wf['id']}").json()["ok"]
    assert ok
    assert not idx.exists()
