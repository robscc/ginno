"""Run-scoped WebSocket (design B P2): snapshot-on-connect (replay from
events.jsonl with a total seq order) + live run.event/run.status frames pushed
to the run channel — independent of any presenting session, so a headless
run has a live channel too."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _human_gate_dsl():
    return {
        "name": "RunWS",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "h", "type": "human", "question": "继续吗？"},
                {"id": "s2", "type": "llm", "prompt": "two"},
            ],
            "edges": [{"from": "s1", "to": "h"}, {"from": "h", "to": "s2"}],
        },
    }


def test_run_ws_snapshot_and_live_push(client, monkeypatch):
    wf = store.create_def(_human_gate_dsl())
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="one"), script(text="two")]),
    )
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "paused"  # parked at the human node

    with client.websocket_connect(f"/api/ws/runs/{rid}") as ws:
        snap = ws.receive_json()
        assert snap["event"] == "run.snapshot"
        assert snap["run"]["id"] == rid and snap["run"]["status"] == "paused"
        assert snap["run"]["pending_interrupt"]["kind"] == "human"
        kinds = [e["kind"] for e in snap["events"]]
        assert "node_enter" in kinds and "interrupt" in kinds
        seqs = [e["seq"] for e in snap["events"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        last_seq = snap["last_seq"]
        assert last_seq == max(seqs)

        # Resume from INSIDE the socket context: the driver's frames must land
        # on this run-scoped socket (no session involved anywhere).
        r = client.post(f"/api/workflow_runs/{rid}/resume", json={"answer": "go"})
        assert r.status_code == 200

        frames = []
        while True:
            f = ws.receive_json()
            frames.append(f)
            # run.status(done) is immediately followed by its run.snapshot
            # refresh — wait for that pair, not just the status word.
            if f["event"] == "run.snapshot" and (f.get("run") or {}).get("status") in ("done", "failed"):
                break
        live_kinds = [f["payload"]["kind"] for f in frames if f["event"] == "run.event"]
        assert "resume" in live_kinds and "node_exit" in live_kinds and "done" in live_kinds
        # live frames continue the snapshot's seq order (no gaps, no restart)
        live_seqs = [f["payload"]["seq"] for f in frames if f["event"] == "run.event"]
        assert live_seqs == sorted(live_seqs)
        assert all(s > last_seq for s in live_seqs)
        # every status transition also refreshes the run JSON on this channel
        snaps = [f for f in frames if f["event"] == "run.snapshot" and "run" in f]
        assert snaps and snaps[-1]["run"]["status"] == "done"


def test_run_ws_unknown_run_closes_cleanly(client):
    with client.websocket_connect("/api/ws/runs/zzz") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "run.missing"


def test_resume_pushes_running_status_on_session_channel(client, monkeypatch):
    """paused→running must be PUSHED, not just persisted: the chat run card
    renders from session-channel frames (the right dock polls instead), so a
    resume that stays silent leaves the card showing 已暂停 until the next
    terminal push. The resume driver must announce "running" FIRST."""
    wf = store.create_def(_human_gate_dsl())
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="one"), script(text="two")]),
    )
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: ScriptedChatModel(scripts=[])
    )
    sid = client.post("/api/sessions", json={
        "project_slug": "default", "workspace": "/tmp/gw", "agent_id": "dev",
    }).json()["id"]
    rid = client.post("/api/workflow_runs", json={
        "workflow_id": wf["id"], "session_id": sid, "present_in_session_id": sid,
    }).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "paused"  # parked at the human node

    with client.websocket_connect(f"/api/ws/sessions/{sid}") as ws:
        r = client.post(f"/api/workflow_runs/{rid}/resume", json={"answer": "go"})
        assert r.status_code == 200

        statuses = []
        while True:
            f = ws.receive_json()
            if f.get("event") == "run.status":
                statuses.append(f["status"])
                if f["status"] in ("done", "failed", "cancelled", "interrupted"):
                    break
        assert statuses, "resume produced no run.status frames on the session channel"
        # the paused→running flip arrives, and BEFORE the terminal status
        assert statuses[0] == "running", statuses
        assert statuses[-1] == "done", statuses


def test_append_event_seq_survives_restart_sim(client):
    """seq is seeded from the file line count: appending after a simulated
    restart continues the sequence instead of restarting at 1."""
    from ginno_runtime.workflows import events as wf_events

    e1 = wf_events.append_event("seqrun", "node_enter", node_id="a")
    e2 = wf_events.append_event("seqrun", "node_enter", node_id="b")
    assert e2["seq"] == e1["seq"] + 1
    wf_events._SEQ.pop("seqrun", None)  # simulate a sidecar restart
    e3 = wf_events.append_event("seqrun", "node_exit", node_id="b")
    assert e3["seq"] > e2["seq"]
    # read back synthesizes seq for legacy lines without one
    import json
    from ginno_runtime import paths

    p = paths.home() / "workflow_runs" / "legacy.events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join([
        json.dumps({"ts": 1, "run_id": "legacy", "kind": "node_enter"}),
        json.dumps({"ts": 2, "run_id": "legacy", "kind": "node_exit"}),
    ]))
    legacy = wf_events.read_events("legacy")
    assert [e["seq"] for e in legacy] == [1, 2]


def test_resume_pins_the_runs_dsl_version(client, monkeypatch):
    """继续执行必须用 run 钉住的版本快照（2026-09-25：run bb1b394c 在 v5 编辑后
    被 resume，半段节点在新图上重跑、步骤表错乱）。v2 重命名了暂停节点 →
    resume 仍按 v1 的图走完；版本被删 → 明确 failed。"""
    wf = store.create_def(_human_gate_dsl())
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="one"), script(text="two")]),
    )
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    assert client.post(f"/api/workflow_runs/{rid}/_await").json()["run"]["status"] == "paused"

    # v2 renames the parked human node h → h2 (graph topology changed)
    v2 = {
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "h2", "type": "human", "question": "?"},
                {"id": "s2", "type": "llm", "prompt": "two"},
            ],
            "edges": [{"from": "s1", "to": "h2"}, {"from": "h2", "to": "s2"}],
        }
    }
    assert client.put(f"/api/workflows/{wf['id']}", json=v2).json()["workflow"]["version"] == 2

    r = client.post(f"/api/workflow_runs/{rid}/resume", json={"answer": "go"})
    assert r.status_code == 200
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "done", aw
    enters = [e.get("node_id") for e in aw.get("events", [])] if False else None
    evs = client.get(f"/api/workflow_runs/{rid}/events").json()["events"]
    entered = [e.get("node_id") for e in evs if e.get("kind") == "node_enter"]
    # v1 ids (h, s2) — the v2 rename (h2) never appears on this run
    assert "h2" not in entered and "s2" in entered


def test_resume_with_deleted_version_fails_clearly(client, monkeypatch):
    wf = store.create_def(_human_gate_dsl())
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text="one"), script(text="two")]),
    )
    rid = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    assert client.post(f"/api/workflow_runs/{rid}/_await").json()["run"]["status"] == "paused"
    # forge a dangling pin: the version file disappears (rollback never deletes;
    # simulate by pointing the run at a version that was never written)
    from ginno_runtime.workflows import store as st
    rj = st.get_run(rid)
    rj["dsl_version"] = 99
    st._write_json(st._run_path(rid), rj)
    assert client.post(f"/api/workflow_runs/{rid}/resume", json={"answer": "go"}).status_code == 200
    aw = client.post(f"/api/workflow_runs/{rid}/_await").json()
    assert aw["run"]["status"] == "failed"
    assert "no longer exists" in (aw["run"].get("error") or "")
