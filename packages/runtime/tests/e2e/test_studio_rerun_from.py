"""rerun_from(node_id) Studio e2e (方案B 屏2「从节点重跑」): fork a NEW run that
re-executes from an arbitrary node the source had scheduled.

Mechanics under test (against the REAL engine + file checkpointer): the
source's checkpoint file is cloned+retagged under the fork's run id, the fork
driver walks the state history (newest first) for the last snapshot where the
target node was in ``next``, truncates the record so that snapshot is head
(with its pending_writes stripped, else langgraph would skip the node as
already done), then continues — only the target and its suffix re-execute,
compiled against the source run's PINNED dsl_version.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script_raise

import studio_lib as L

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# disk helpers (run JSON lives in $GINNO_HOME/workflow_runs/, checkpoints in
# the default project's sessions dir under the run id)
# --------------------------------------------------------------------------- #
def _run_json(run_id: str):
    return paths.home() / "workflow_runs" / f"{run_id}.json"


def _ckpt(run_id: str):
    return paths.project_sessions_dir("default") / f"{run_id}.json"


def _edit_run_json(run_id: str, mutate) -> None:
    p = _run_json(run_id)
    rec = json.loads(p.read_text())
    mutate(rec)
    p.write_text(json.dumps(rec, ensure_ascii=False))


def _rerun(client, src_id: str, node_id: str):
    return client.post(f"{L.API}/workflow_runs/{src_id}/rerun_from", json={"node_id": node_id})


def _fork(client, src_id: str, node_id: str, want_status: str = "done") -> dict:
    """rerun_from + await the fork; returns the finished fork run."""
    r = _rerun(client, src_id, node_id)
    assert r.status_code == 200, r.text
    fork = L.await_run(client, r.json()["run"]["id"])
    assert fork["status"] == want_status, fork
    return fork


# --------------------------------------------------------------------------- #
# baseline: dsl_linear(3) with scripts ["one","two","three"] → done
# --------------------------------------------------------------------------- #
def _baseline(client, monkeypatch, scripts=None):
    wf = L.make_wf(client, L.dsl_linear(3))
    L.patch_model(monkeypatch, scripts or ["one", "two", "three"])
    rid = L.run_wf(client, wf["id"])
    src = L.await_run(client, rid)
    assert src["status"] == "done", src
    return wf, rid, src


# --------------------------------------------------------------------------- #
# 01–03: prefix skipping / full rerun / last node
# --------------------------------------------------------------------------- #
def test_01_rerun_from_middle_skips_prefix(client, monkeypatch):
    _, src_id, src = _baseline(client, monkeypatch)
    L.patch_model(monkeypatch, ["two-again", "three-again"])
    fork = _fork(client, src_id, "s2")
    assert L.enters(L.evs(client, fork["id"])) == ["s2", "s3"]  # prefix skipped
    steps = L.steps(fork)
    assert steps["s1"] == "done" and steps["s2"] == "done" and steps["s3"] == "done"
    assert fork["retried_from"] == src_id


def test_02_rerun_from_first_reruns_everything(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    L.patch_model(monkeypatch, ["one-again", "two-again", "three-again"])
    fork = _fork(client, src_id, "s1")
    assert L.enters(L.evs(client, fork["id"])) == ["s1", "s2", "s3"]


def test_03_rerun_from_last_node_only(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    L.patch_model(monkeypatch, ["three-again"])
    fork = _fork(client, src_id, "s3")
    assert L.enters(L.evs(client, fork["id"])) == ["s3"]


# --------------------------------------------------------------------------- #
# 04–05: pinned dsl_version
# --------------------------------------------------------------------------- #
def test_04_fork_compiles_the_pinned_version(client, monkeypatch):
    wf, src_id, _ = _baseline(client, monkeypatch)
    # v2 renames s2 → renamed (new edges); the fork must still speak v1
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
    r = client.put(f"{L.API}/workflows/{wf['id']}", json=v2)
    assert r.status_code == 200 and r.json()["workflow"]["version"] == 2

    L.patch_model(monkeypatch, ["two-again", "three-again"])
    fork = _fork(client, src_id, "s2")
    assert fork["dsl_version"] == 1
    assert L.enters(L.evs(client, fork["id"])) == ["s2", "s3"]  # v1 ids, not renamed


def test_05_pinned_version_gone_is_409(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    _edit_run_json(src_id, lambda rec: rec.update(dsl_version=99))
    r = _rerun(client, src_id, "s1")
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# 06–07: source-status guards
# --------------------------------------------------------------------------- #
def test_06_running_source_is_409(client, monkeypatch):
    """A live running source is racy, so simulate the guard directly: flip a
    DONE run's on-disk status to "running" (no live task behind it) — the
    endpoint must still refuse to fork it."""
    _, src_id, _ = _baseline(client, monkeypatch)
    _edit_run_json(src_id, lambda rec: rec.update(status="running"))
    r = _rerun(client, src_id, "s1")
    assert r.status_code == 409, r.text


def test_07_paused_source_fork_re_parks_at_human(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_with_human())
    L.patch_model(monkeypatch, ["one"])
    rid = L.run_wf(client, wf["id"])
    src = L.await_run(client, rid)
    assert src["status"] == "paused"  # parked at the human node (not "running")
    L.patch_model(monkeypatch, [])
    fork = _fork(client, rid, "h", want_status="paused")
    pi = fork.get("pending_interrupt") or {}
    assert pi.get("kind") == "human"


# --------------------------------------------------------------------------- #
# 08–11: request guards
# --------------------------------------------------------------------------- #
def test_08_unknown_node_is_409(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    r = _rerun(client, src_id, "nope")
    assert r.status_code == 409, r.text


def test_09_never_scheduled_node_forks_then_fails(client, monkeypatch):
    """go_b exists in the DSL but the source's flag routed to go_a — the fork
    is created (200) but then fails with the no-checkpoint-to-rewind error."""
    wf = L.make_wf(client, L.dsl_branch())
    L.patch_model(monkeypatch, [L.wj({"flag": "go"}), "route a"])
    rid = L.run_wf(client, wf["id"])
    src = L.await_run(client, rid)
    assert src["status"] == "done"
    assert "go_b" not in L.enters(L.evs(client, rid))

    r = _rerun(client, rid, "go_b")
    assert r.status_code == 200, r.text
    fork = L.await_run(client, r.json()["run"]["id"])
    assert fork["status"] == "failed", fork
    errs = [e for e in L.evs(client, fork["id"]) if e.get("kind") == "error"]
    assert errs and "检查点" in errs[-1].get("error", "")


def test_10_no_checkpoint_is_409(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    _ckpt(src_id).unlink()
    r = _rerun(client, src_id, "s1")
    assert r.status_code == 409, r.text


def test_11_missing_node_id_is_400(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    r = client.post(f"{L.API}/workflow_runs/{src_id}/rerun_from", json={})
    assert r.status_code == 400, r.text


# --------------------------------------------------------------------------- #
# 12–14: control-flow fidelity
# --------------------------------------------------------------------------- #
def test_12_fork_from_loop_body_completes(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_loop())
    L.patch_model(monkeypatch, ["b1", "b2", "b3", "use"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"

    # last scheduling of the body wins: b re-runs (for the last item) then use
    L.patch_model(monkeypatch, ["b-again", "use-again"])
    fork = _fork(client, rid, "b")
    body = [n for n in L.enters(L.evs(client, fork["id"])) if n == "b"]
    assert len(body) >= 1
    assert fork["status"] == "done"


def test_13_fork_reruns_the_writes_extract(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(2, writes_first=True))
    L.patch_model(monkeypatch, [L.wj({"items": ["x", "y"]}), "b"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"

    L.patch_model(monkeypatch, [L.wj({"items": ["p", "q"]}), "b-again"])
    fork = _fork(client, rid, "s1")
    kinds = L.kinds(L.evs(client, fork["id"]))
    assert "context_write" in kinds  # the extract fast path re-ran on the fork


def test_14_fork_branch_is_deterministic_again(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_branch())
    L.patch_model(monkeypatch, [L.wj({"flag": "go"}), "route a"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"
    assert L.enters(L.evs(client, rid))[-1] == "go_a"

    # same flag write → the fork takes go_a again, never the default arm
    L.patch_model(monkeypatch, [L.wj({"flag": "go"}), "route a again"])
    fork = _fork(client, rid, "s1")
    entered = L.enters(L.evs(client, fork["id"]))
    assert entered[-1] == "go_a"
    assert "go_b" not in entered


# --------------------------------------------------------------------------- #
# 15–16: healing + retry provenance
# --------------------------------------------------------------------------- #
def test_15_heal_failed_node_via_rerun(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(3))
    L.patch_model(monkeypatch, ["one", script_raise(RuntimeError("provider boom"))])
    rid = L.run_wf(client, wf["id"])
    src = L.await_run(client, rid)
    assert src["status"] == "failed", src
    assert L.steps(src).get("s2") == "failed"

    # rerun from the failed node with healthy scripts → the fork finishes
    L.patch_model(monkeypatch, ["two-again", "three-again"])
    fork = _fork(client, rid, "s2")
    assert L.enters(L.evs(client, fork["id"])) == ["s2", "s3"]


def test_16_source_retry_run_id_tracks_latest_fork(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    L.patch_model(monkeypatch, ["two-again", "three-again"])
    fork1 = _fork(client, src_id, "s2")
    assert L.get_run(client, src_id)["retry_run_id"] == fork1["id"]

    # rerunning the same (done) source again is allowed — latest fork wins
    L.patch_model(monkeypatch, ["two-more", "three-more"])
    fork2 = _fork(client, src_id, "s2")
    assert fork2["id"] != fork1["id"]
    assert L.get_run(client, src_id)["retry_run_id"] == fork2["id"]


# --------------------------------------------------------------------------- #
# 17–18: carried inputs / clean event stream
# --------------------------------------------------------------------------- #
def test_17_context_override_is_carried(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_template("who"))
    L.patch_model(monkeypatch, [L.wj({"who": "zz"}), "t2"])
    rid = L.run_wf(client, wf["id"], context_override={"who": "zz"})
    assert L.await_run(client, rid)["status"] == "done"

    L.patch_model(monkeypatch, ["t2-again"])
    fork = _fork(client, rid, "s2")
    assert fork.get("context_override") == {"who": "zz"}


def test_18_fork_event_stream_starts_clean(client, monkeypatch):
    _, src_id, _ = _baseline(client, monkeypatch)
    L.patch_model(monkeypatch, ["two-again", "three-again"])
    fork = _fork(client, src_id, "s2")
    evs = L.evs(client, fork["id"])
    assert evs and evs[0]["seq"] == 1  # fresh stream, not a continuation
    seqs = [e["seq"] for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # nothing from the source's history leaks in: only the re-executed nodes
    assert set(L.enters(evs)) == {"s2", "s3"}


# --------------------------------------------------------------------------- #
# 19–20: deleted workflow / supervisor gate
# --------------------------------------------------------------------------- #
def test_19_deleted_workflow_is_404(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(2))
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"
    # own def (not system) → deletable
    r = client.delete(f"{L.API}/workflows/{wf['id']}")
    assert r.status_code == 200 and r.json().get("ok"), r.text
    r = _rerun(client, rid, "s1")
    assert r.status_code == 404, r.text


def test_20_fork_parks_at_supervisor_gate_again(client, monkeypatch):
    wf = L.make_wf(client, L.gated(L.dsl_linear(2), after=["s1"]))
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])
    src = L.await_run(client, rid)
    assert src["status"] == "paused"
    assert (L.decide(client, rid, "continue")).status_code == 200
    assert L.await_run(client, rid)["status"] == "done"

    # the fork re-runs s1 → the human-mode gate fires again
    L.patch_model(monkeypatch, ["one-again"])
    fork = _fork(client, rid, "s1", want_status="paused")
    pi = fork.get("pending_interrupt") or {}
    assert pi.get("kind") == "supervisor"
