"""Studio run-lifecycle e2e (方案B P1+P2): trigger → execute → retry/cancel/delete.

Drives the REAL engine through the HTTP API with a ScriptedChatModel patched
into the workflow driver (see ``studio_lib``). Covers: linear/branch/loop
execution, writes fast path, context_override, session binding, trigger
validation, soft/hard failure, retry, cancel, delete + artifact cleanup,
bulk cleanup, event filters, unknown-run 404s and startup orphan
reconciliation.
"""

from __future__ import annotations

import json

import pytest

import studio_lib as L
from ginno_runtime.testing.fake_model import script_raise

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# 1-4: happy paths
# --------------------------------------------------------------------------- #
def test_01_linear_two_nodes_done(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    wf = L.make_wf(client, L.dsl_linear(2))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    st = L.steps(run)
    assert st["s1"] == "done" and st["s2"] == "done", st
    events = L.evs(client, run_id)
    assert L.kinds(events)[-1] == "done"
    assert L.enters(events) == ["s1", "s2"]


def test_02_writes_fast_path_extract_step_and_context_write(client, monkeypatch):
    L.patch_model(monkeypatch, [L.wj({"items": ["x", "y"]}), "two"])
    wf = L.make_wf(client, L.dsl_linear(2, writes_first=True))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    st = L.steps(run)
    assert "s1__extract" in st, st
    writes = [e for e in L.evs(client, run_id) if e.get("kind") == "context_write"]
    assert any(list(e.get("keys") or []) == ["items"] for e in writes), writes


def test_03_context_override_renders_title_and_persists(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    doc = {
        "name": "OVR",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "title": "hello {{who}}", "prompt": "p1"},
                {"id": "s2", "type": "llm", "prompt": "p2"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        },
    }
    wf = L.make_wf(client, doc)
    run_id = L.run_wf(client, wf["id"], context_override={"who": "zz"})
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    titles = {s["id"]: s.get("title") for s in run["steps"]}
    assert titles["s1"] == "hello zz", titles
    assert run.get("context_override") == {"who": "zz"}, run.get("context_override")


def test_04_run_binds_session(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf = L.make_wf(client, L.dsl_linear(1))
    run_id = L.run_wf(client, wf["id"], session_id="sess-1")
    run = L.await_run(client, run_id)

    assert run["session_id"] == "sess-1", run
    assert run["present_in_session_id"] == "sess-1", run


# --------------------------------------------------------------------------- #
# 5-6: trigger validation
# --------------------------------------------------------------------------- #
def test_05_create_run_without_workflow_id_400(client):
    r = client.post("/api/workflow_runs", json={})
    assert r.status_code == 400, r.text


def test_06_create_run_unknown_workflow_404(client):
    r = client.post("/api/workflow_runs", json={"workflow_id": "no-such-wf"})
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------------- #
# 7-10: branch + loop
# --------------------------------------------------------------------------- #
def test_07_branch_case_path(client, monkeypatch):
    L.patch_model(monkeypatch, [L.wj({"flag": "go"}), "route a"])
    wf = L.make_wf(client, L.dsl_branch("go"))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    events = L.evs(client, run_id)
    assert L.enters(events)[-1] == "go_a", L.enters(events)
    assert "go_b" not in L.enters(events)


def test_08_branch_default_path(client, monkeypatch):
    L.patch_model(monkeypatch, [L.wj({"flag": "nope"}), "route b"])
    wf = L.make_wf(client, L.dsl_branch("go"))  # case wants "go", value is "nope"
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    events = L.evs(client, run_id)
    assert L.enters(events)[-1] == "go_b", L.enters(events)
    assert "go_a" not in L.enters(events)


def test_09_loop_three_items(client, monkeypatch):
    L.patch_model(monkeypatch, ["i1", "i2", "i3", "use"])
    wf = L.make_wf(client, L.dsl_loop(items=[{"name": "a"}, {"name": "b"}, {"name": "c"}]))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    events = L.evs(client, run_id)
    assert L.enters(events).count("b") == 3, L.enters(events)
    assert sum(1 for e in events if e.get("kind") == "loop_iter") >= 3


def test_10_loop_empty_items_skip(client, monkeypatch):
    L.patch_model(monkeypatch, ["use"])
    wf = L.make_wf(client, L.dsl_loop(items=[], on_empty="skip"))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    assert L.steps(run).get("b") == "skipped", L.steps(run)
    assert any(e.get("kind") == "loop_skip" for e in L.evs(client, run_id))


# --------------------------------------------------------------------------- #
# 11-12: failure modes
# --------------------------------------------------------------------------- #
def _inline_soft_fail_doc() -> dict:
    return {
        "name": "SOFT",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "p1"},
                {"id": "s2", "type": "llm", "prompt": "p2", "on_error": "continue"},
                {"id": "s3", "type": "llm", "prompt": "p3"},
            ],
            "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"}],
        },
    }


def test_11_soft_fail_run_continues_with_warning(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", script_raise(RuntimeError("boom")), "carry"])
    wf = L.make_wf(client, _inline_soft_fail_doc())
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "done", run
    assert run.get("warnings") == 1, run.get("warnings")
    st = L.steps(run)
    assert st["s1"] == "done" and st["s2"] == "failed" and st["s3"] == "done", st


def _inline_hard_fail_doc() -> dict:
    return {
        "name": "HARD",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "p1"},
                {"id": "s2", "type": "llm", "prompt": "p2"},
                {"id": "s3", "type": "llm", "prompt": "p3"},
            ],
            "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "s3"}],
        },
    }


def test_12_hard_fail_mid_run(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", script_raise(RuntimeError("boom")), "three"])
    wf = L.make_wf(client, _inline_hard_fail_doc())
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)

    assert run["status"] == "failed", run
    assert (run.get("error_detail") or {}).get("node_id") == "s2", run.get("error_detail")
    st = L.steps(run)
    assert st["s1"] == "done" and st["s2"] == "failed" and st["s3"] == "pending", st
    errs = [e for e in L.evs(client, run_id) if e.get("kind") == "error"]
    assert errs and any(e.get("traceback") for e in errs), errs


# --------------------------------------------------------------------------- #
# 13-14: retry
# --------------------------------------------------------------------------- #
def test_13_retry_failed_run_creates_new_done_run(client, monkeypatch):
    L.patch_model(monkeypatch, [script_raise(RuntimeError("boom"))])
    wf = L.make_wf(client, L.dsl_linear(1))
    old_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, old_id)["status"] == "failed"

    L.patch_model(monkeypatch, ["fresh"])  # new run replays scripts from 0
    r = client.post(f"/api/workflow_runs/{old_id}/retry")
    assert r.status_code == 200, r.text
    body = r.json()
    new_id = body["run"]["id"]
    assert new_id != old_id
    assert body["run"].get("retried_from") == old_id
    src = L.get_run(client, old_id)
    assert src.get("retry_run_id") == new_id, src
    assert L.await_run(client, new_id)["status"] == "done"


def test_14_retry_done_run_409(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf = L.make_wf(client, L.dsl_linear(1))
    run_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, run_id)["status"] == "done"

    r = client.post(f"/api/workflow_runs/{run_id}/retry")
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# 15-17: cancel / delete / cleanup
# --------------------------------------------------------------------------- #
def _paused_run(client, monkeypatch) -> str:
    L.patch_model(monkeypatch, ["one"])
    wf = L.make_wf(client, L.dsl_with_human())
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)
    assert run["status"] == "paused", run
    return run_id


def test_15_cancel_paused_run_then_resume_409(client, monkeypatch):
    run_id = _paused_run(client, monkeypatch)

    r = client.post(f"/api/workflow_runs/{run_id}/cancel")
    assert r.status_code == 200, r.text
    assert L.get_run(client, run_id)["status"] == "cancelled"

    r = client.post(f"/api/workflow_runs/{run_id}/resume", json={})
    assert r.status_code == 409, r.text


def test_16_delete_run_removes_all_artifacts(client, monkeypatch):
    from ginno_runtime import paths

    run_id = _paused_run(client, monkeypatch)
    client.post(f"/api/workflow_runs/{run_id}/cancel")

    home = paths.home()
    run_json = home / "workflow_runs" / f"{run_id}.json"
    events_jsonl = home / "workflow_runs" / f"{run_id}.events.jsonl"
    checkpoint = paths.project_sessions_dir("default") / f"{run_id}.json"
    assert run_json.exists() and events_jsonl.exists(), (run_json, events_jsonl)

    r = client.delete(f"/api/workflow_runs/{run_id}")
    assert r.status_code == 200, r.text
    assert L.get_run(client, run_id) is None
    assert not run_json.exists()
    assert not events_jsonl.exists()
    assert not checkpoint.exists()


def test_17_cleanup_deletes_done_leaves_paused(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    wf = L.make_wf(client, L.dsl_linear(2))
    done_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, done_id)["status"] == "done"
    paused_id = _paused_run(client, monkeypatch)

    r = client.post("/api/workflow_runs/cleanup", json={"statuses": ["done"]})
    assert r.status_code == 200, r.text
    assert L.get_run(client, done_id) is None
    assert L.get_run(client, paused_id) is not None


# --------------------------------------------------------------------------- #
# 18-20: event filters, unknown ids, orphan reconciliation
# --------------------------------------------------------------------------- #
def test_18_event_filters(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    wf = L.make_wf(client, L.dsl_linear(2))
    run_id = L.run_wf(client, wf["id"])
    L.await_run(client, run_id)

    by_node = L.evs(client, run_id, node_id="s2")
    assert by_node and all(e.get("node_id") == "s2" for e in by_node)

    by_kind = L.evs(client, run_id, kind="node_enter")
    assert by_kind and all(e.get("kind") == "node_enter" for e in by_kind)

    both = L.evs(client, run_id, node_id="s2", kind="node_enter")
    assert [e.get("node_id") for e in both] == ["s2"]


def test_19_unknown_run_id_404_everywhere(client):
    rid = "no-such-run"
    assert client.get(f"/api/workflow_runs/{rid}").status_code == 404
    assert client.post(f"/api/workflow_runs/{rid}/cancel").status_code == 404
    assert client.post(f"/api/workflow_runs/{rid}/pause").status_code == 404
    assert client.post(f"/api/workflow_runs/{rid}/resume", json={}).status_code == 404
    assert client.post(f"/api/workflow_runs/{rid}/decide", json={}).status_code == 404
    r = client.post(f"/api/workflow_runs/{rid}/rerun_from", json={"node_id": "s1"})
    assert r.status_code == 404, r.text
    assert client.delete(f"/api/workflow_runs/{rid}").status_code == 404


def test_20_orphan_reconciliation_flips_running_to_interrupted(client, monkeypatch):
    from ginno_runtime import paths
    from ginno_runtime.api.workflows import _reconcile_orphan_runs

    L.patch_model(monkeypatch, ["one", "two"])
    wf = L.make_wf(client, L.dsl_linear(2))
    run_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, run_id)["status"] == "done"

    # Simulate a crash mid-run: the run JSON survived with status "running".
    p = paths.home() / "workflow_runs" / f"{run_id}.json"
    run = json.loads(p.read_text())
    run["status"] = "running"
    p.write_text(json.dumps(run, ensure_ascii=False))

    _reconcile_orphan_runs()

    healed = L.get_run(client, run_id)
    assert healed["status"] == "interrupted", healed["status"]
    assert any(e.get("kind") == "interrupted" for e in L.evs(client, run_id))
