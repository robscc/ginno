"""Run-lifecycle hardening: never-silent failure, reconciliation, retry, delete,
cleanup (the "stuck run" fix). Complements test_workflow_run*.py."""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import paths, server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.todos import sync_ledger
from ginno_runtime.workflows import events as wf_events
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _step_wf(agent: str = "dev"):
    return {
        "name": "OneStep",
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": agent, "goal": "do it"}],
            "edges": [],
        },
    }


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
        lambda *a, **k: ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"x": 1}')]),
    )


# --- dependency-build failures land as a visible failed run, not "running" ---


def test_dep_build_failure_marks_failed_with_error(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf(agent="ghost"))
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    assert "ghost" in (run.get("error") or ""), run
    kinds = [e["kind"] for e in client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]]
    assert "error" in kinds
    # the bg task was pruned from the registry
    assert run_id not in server._WF_RUN_TASKS


def test_build_model_failure_marks_failed(client, monkeypatch):
    wf = store.create_def(_step_wf())
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", _raise_disabled)
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    assert "disabled" in (run.get("error") or ""), run


def _raise_disabled(*a, **k):
    raise ValueError("provider x is disabled (enable it in Settings)")


def test_build_deps_missing_workflow_returns_6_tuple():
    result = server._wf_build_deps("r", "nope")
    assert result == (None, None, None, None, None, None)


# --- startup reconciliation heals orphaned / historical runs ---


def test_reconcile_orphan_runs(isolated_home, monkeypatch):
    paths.ensure_layout()
    wf = store.create_def(_step_wf())
    # a run abandoned mid-flight by a crash/quit
    stuck = store.create_run(wf)
    assert stuck["status"] == "running"
    # a paused run must survive reconciliation (still resumable)
    paused = store.create_run(wf)
    server._set_run_status(paused["id"], "paused")
    # a historical failed run whose error only lives in the events log
    failed = store.create_run(wf)
    server._set_run_status(failed["id"], "failed")
    wf_events.append_event(failed["id"], "error", error="RateLimitError: 429 too many")

    server._reconcile_orphan_runs()

    assert store.get_run(stuck["id"])["status"] == "interrupted"
    assert store.get_run(stuck["id"])["error"]
    assert any(e["kind"] == "interrupted" for e in wf_events.read_events(stuck["id"]))
    assert store.get_run(paused["id"])["status"] == "paused"
    got_failed = store.get_run(failed["id"])
    assert got_failed["status"] == "failed"
    assert got_failed["error"] == "RateLimitError: 429 too many"


# --- retry ---


def test_retry_cancelled_run_carries_context_and_binding(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    a = client.post(
        "/api/workflow_runs",
        json={"workflow_id": wf["id"], "session_id": "sess-1",
              "present_in_session_id": "sess-1", "context_override": {"x": 1}},
    ).json()["run"]
    client.post(f"/api/workflow_runs/{a['id']}/_await")  # pause at human
    assert client.post(f"/api/workflow_runs/{a['id']}/cancel").status_code == 200
    assert store.get_run(a["id"])["status"] == "cancelled"

    r = client.post(f"/api/workflow_runs/{a['id']}/retry")
    assert r.status_code == 200, r.text
    b = r.json()["run"]
    assert r.json()["source_run_id"] == a["id"]
    assert b["retried_from"] == a["id"]
    assert b["context_override"] == {"x": 1}
    assert b["session_id"] == "sess-1"
    assert b["present_in_session_id"] == "sess-1"
    assert store.get_run(a["id"])["retry_run_id"] == b["id"]

    # the retry is executable: it pauses at the same human node, then finishes
    client.post(f"/api/workflow_runs/{b['id']}/_await")
    assert store.get_run(b["id"])["status"] == "paused"
    client.post(f"/api/workflow_runs/{b['id']}/decide", json={"decision": "continue"})
    aw = client.post(f"/api/workflow_runs/{b['id']}/_await").json()
    assert aw["run"]["status"] == "done", aw


def test_retry_status_guards(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # done
    assert client.post(f"/api/workflow_runs/{run_id}/retry").status_code == 409
    assert client.post("/api/workflow_runs/nope/retry").status_code == 404


# --- delete ---


def test_delete_run_removes_all_artifacts(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # done

    run_path = paths.home() / "workflow_runs" / f"{run_id}.json"
    ev_path = paths.home() / "workflow_runs" / f"{run_id}.events.jsonl"
    ckpt = server._run_checkpoint_path(run_id)
    assert run_path.exists() and ev_path.exists()

    assert client.delete(f"/api/workflow_runs/{run_id}").status_code == 200
    assert not run_path.exists()
    assert not ev_path.exists()
    assert not ckpt.exists()
    assert client.get(f"/api/workflow_runs/{run_id}").status_code == 404


def test_delete_missing_run_404(client):
    assert client.delete("/api/workflow_runs/nope").status_code == 404


def test_delete_running_run_cancels_and_does_not_resurrect(client, monkeypatch):
    import time as _t

    wf = store.create_def(_step_wf())

    async def _hang(
        dsl, *, run_id, model, tools, context_override=None, project_slug="default", usage_attr=None
    ):
        # an engine that never produces a terminal event
        await asyncio.sleep(30)
        yield {"run_id": run_id, "kind": "done"}

    from ginno_runtime.workflows import engine as wf_engine

    monkeypatch.setattr(wf_engine, "run_workflow", _hang)
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    # let the bg task start and park in the sleeping engine
    for _ in range(50):
        if server._WF_RUN_TASKS.get(run_id):
            break
        _t.sleep(0.02)
    assert client.delete(f"/api/workflow_runs/{run_id}").status_code == 200
    # give the cancelled task a moment to unwind, then confirm no resurrection
    _t.sleep(0.2)
    assert not (paths.home() / "workflow_runs" / f"{run_id}.json").exists()
    assert client.get(f"/api/workflow_runs/{run_id}").status_code == 404


# --- cleanup ---


def test_cleanup_deletes_terminal_only(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf())
    done = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]
    client.post(f"/api/workflow_runs/{done['id']}/_await")  # done
    paused = store.create_run(wf)
    server._set_run_status(paused["id"], "paused")

    # orphan events file with no run json -> swept
    orphan = paths.home() / "workflow_runs" / "deadbeef00.events.jsonl"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("{}\n")

    r = client.post("/api/workflow_runs/cleanup", json={})
    assert r.status_code == 200
    assert r.json()["deleted"] == 1  # only the done run (paused is not terminal)
    assert store.get_run(done["id"]) is None
    assert store.get_run(paused["id"])["status"] == "paused"
    assert not orphan.exists()


def test_cleanup_respects_status_filter(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf())
    done = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]
    client.post(f"/api/workflow_runs/{done['id']}/_await")
    r = client.post("/api/workflow_runs/cleanup", json={"statuses": ["failed"]})
    assert r.json()["deleted"] == 0
    assert store.get_run(done["id"]) is not None


# --- cancel records event + ledger ---


def test_cancel_writes_event_and_ledger(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    sync_ledger.append("todo-1", "prov", "ext-1", "push", run_id)
    client.post(f"/api/workflow_runs/{run_id}/_await")  # pause
    assert client.post(f"/api/workflow_runs/{run_id}/cancel").status_code == 200
    kinds = [e["kind"] for e in wf_events.read_events(run_id)]
    assert "cancelled" in kinds
    entry = next(e for e in sync_ledger.latest() if e["run_id"] == run_id)
    assert entry["status"] == "cancelled"
    assert store.get_run(run_id)["error"] == "cancelled by user"


# --- misc ---


def test_get_missing_run_404(client):
    assert client.get("/api/workflow_runs/nope").status_code == 404


def test_context_override_persisted_on_create(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_step_wf())
    run_id = client.post(
        "/api/workflow_runs", json={"workflow_id": wf["id"], "context_override": {"k": "v"}}
    ).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")
    assert client.get(f"/api/workflow_runs/{run_id}").json()["run"]["context_override"] == {"k": "v"}
