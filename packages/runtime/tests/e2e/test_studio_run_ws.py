"""Studio e2e: the run-scoped WebSocket (方案B 阶段2).

Covers the full ``WS /api/ws/runs/{run_id}`` contract against the REAL engine:

* snapshot-on-connect (replay from events.jsonl, total seq order, legacy and
  corrupt lines tolerated);
* live ``run.event`` / ``run.status`` + terminal ``run.snapshot`` refresh on
  resume / cancel / failure — including for HEADLESS runs (no session);
* multiple sockets per run, socket-close harmlessness, per-run isolation,
  reconnect replay and seq continuity across a simulated sidecar restart.

WS discipline: TestClient receives have NO timeout, so every drain goes through
``studio_lib.ws_drain`` and ends on a frame that CERTAINLY arrives (the
``run.snapshot`` pushed right after a terminal ``run.status``).
"""

from __future__ import annotations

import json

import pytest
from ginno_runtime.testing.fake_model import script_raise

import studio_lib as L

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _events_path(run_id: str):
    from ginno_runtime import paths

    return paths.home() / "workflow_runs" / f"{run_id}.events.jsonl"


def _park(client, monkeypatch, question: str = "继续吗？") -> str:
    """Create + trigger a dsl_with_human run and park it at the human node.
    Returns the run id (deterministically paused)."""
    wf = L.make_wf(client, L.dsl_with_human(question=question))
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])  # headless: no session_id anywhere
    run = L.await_run(client, rid)
    assert run["status"] == "paused", run
    return rid


def _open(client, run_id: str):
    return client.websocket_connect(f"/api/ws/runs/{run_id}")


def _snap(client, ws, run_id: str) -> dict:
    """Consume the mandatory first frame: the connect-time snapshot."""
    snap = ws.receive_json()
    assert snap["event"] == "run.snapshot", snap
    assert snap["run_id"] == run_id
    return snap


def _run_events(frames: list) -> list:
    return [f["payload"] for f in frames if f["event"] == "run.event"]


# --------------------------------------------------------------------------- #
# 1-5: snapshot + the three terminal flows
# --------------------------------------------------------------------------- #
def test_01_snapshot_on_paused_run(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
        assert snap["run"]["id"] == rid
        assert snap["run"]["status"] == "paused"
        assert snap["run"]["pending_interrupt"]["kind"] == "human"
        kinds = L.kinds(snap["events"])
        assert "node_enter" in kinds and "interrupt" in kinds
        seqs = [e["seq"] for e in snap["events"]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        assert snap["last_seq"] == max(seqs)


def test_02_live_resume_frames_continue_seq(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
        last_seq = snap["last_seq"]

        r = L.resume(client, rid, {"answer": "go"})
        assert r.status_code == 200, r.text

        frames = L.ws_drain(ws, L.is_snapshot_with("done"))
    live = _run_events(frames)
    live_seqs = [e["seq"] for e in live]
    live_kinds = [e["kind"] for e in live]
    assert live_seqs == sorted(live_seqs) and len(set(live_seqs)) == len(live_seqs)
    assert all(s > last_seq for s in live_seqs)
    assert "resume" in live_kinds and "node_exit" in live_kinds and "done" in live_kinds


def test_03_done_status_immediately_followed_by_snapshot(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        _snap(client, ws, rid)
        L.resume(client, rid, {"answer": "go"})
        frames = L.ws_drain(ws, L.is_snapshot_with("done"))
    statuses = [i for i, f in enumerate(frames) if f["event"] == "run.status" and f.get("status") == "done"]
    assert statuses, frames
    i = statuses[-1]
    assert i + 1 < len(frames)
    nxt = frames[i + 1]
    assert nxt["event"] == "run.snapshot"
    assert (nxt.get("run") or {}).get("status") == "done"


def test_04_cancel_flow(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        _snap(client, ws, rid)
        r = client.post(f"{L.API}/workflow_runs/{rid}/cancel")
        assert r.status_code == 200, r.text
        frames = L.ws_drain(ws, L.is_snapshot_with("cancelled"))
    # terminal signal on the channel: run.status frame + its snapshot refresh
    assert any(f["event"] == "run.status" and f.get("status") == "cancelled" for f in frames)
    snap = [f for f in frames if f["event"] == "run.snapshot"][-1]
    assert snap["run"]["status"] == "cancelled"
    # the cancelled event rides the run channel like every engine event
    # (pushed by the cancel endpoint since the P2 fix — it used to land only
    # in events.jsonl, invisible to live clients).
    assert any(
        f["event"] == "run.event" and f["payload"].get("kind") == "cancelled" for f in frames
    )
    assert "cancelled" in L.kinds(L.evs(client, rid))
    assert L.get_run(client, rid)["status"] == "cancelled"


def test_05_failure_flow(client, monkeypatch):
    # human-first DSL: the run parks WITHOUT any model call, so the failure can
    # be scripted for the RESUME — which happens while the socket is open (an
    # instantly-failing trigger could never be observed live: the run id only
    # exists after the POST that already started it).
    wf = L.make_wf(client, {
        "name": "WSFail",
        "dsl": {
            "entry": "h",
            "nodes": [
                {"id": "h", "type": "human", "question": "go?"},
                {"id": "s1", "type": "llm", "prompt": "boom"},
            ],
            "edges": [{"from": "h", "to": "s1"}],
        },
    })
    L.patch_model(monkeypatch, [script_raise(RuntimeError("provider down"))])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "paused"

    with _open(client, rid) as ws:
        _snap(client, ws, rid)
        r = L.resume(client, rid, {"answer": "go"})
        assert r.status_code == 200, r.text
        frames = L.ws_drain(ws, L.is_snapshot_with("failed"))
    err_events = [e for e in _run_events(frames) if e["kind"] == "error"]
    assert any("provider down" in (e.get("error") or "") for e in err_events), frames
    fail_idx = [i for i, f in enumerate(frames) if f["event"] == "run.status" and f.get("status") == "failed"]
    assert fail_idx, frames
    # empirically the engine's error event lands BEFORE the failed status (the
    # driver marks the run failed from the terminal event, not after it)
    assert err_events and _run_events(frames).index(err_events[0]) < fail_idx[0]
    assert [f for f in frames if f["event"] == "run.snapshot"][-1]["run"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# 6-9: protocol + multi-socket
# --------------------------------------------------------------------------- #
def test_06_ping_pong(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        _snap(client, ws, rid)
        ws.send_json({"type": "ping"})
        frames = L.ws_drain(ws, lambda f: f.get("event") == "run.pong", max_frames=5)
    assert frames[-1]["event"] == "run.pong"


def test_07_unknown_run_missing_then_close(client):
    with _open(client, "zzz") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "run.missing"
        assert msg["run_id"] == "zzz"


def test_08_two_sockets_same_live_frames(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws1, _open(client, rid) as ws2:
        assert _snap(client, ws1, rid)["last_seq"] == _snap(client, ws2, rid)["last_seq"]
        L.resume(client, rid, {"answer": "go"})
        f1 = L.ws_drain(ws1, L.is_snapshot_with("done"))
        f2 = L.ws_drain(ws2, L.is_snapshot_with("done"))
    seqs1 = [e["seq"] for e in _run_events(f1)]
    seqs2 = [e["seq"] for e in _run_events(f2)]
    assert seqs1 and seqs1 == seqs2


def test_09_socket_close_is_harmless(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws1:
        _snap(client, ws1, rid)
        ws2 = _open(client, rid).__enter__()
        _snap(client, ws2, rid)
        # ws1 closed here; ws2 stays open across the resume
    L.resume(client, rid, {"answer": "go"})
    frames = L.ws_drain(ws2, L.is_snapshot_with("done"))
    ws2.__exit__(None, None, None)
    assert any(e["kind"] == "done" for e in _run_events(frames))
    assert L.get_run(client, rid)["status"] == "done"


# --------------------------------------------------------------------------- #
# 10-13: snapshot edge cases
# --------------------------------------------------------------------------- #
def test_10_headless_run_has_live_channel(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_with_human())
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])  # NO session_id — headless
    assert L.await_run(client, rid)["status"] == "paused"
    assert L.get_run(client, rid)["present_in_session_id"] is None

    with _open(client, rid) as ws:
        _snap(client, ws, rid)
        L.resume(client, rid, {"answer": "go"})
        frames = L.ws_drain(ws, L.is_snapshot_with("done"))
    assert any(e["kind"] == "done" for e in _run_events(frames))


def test_11_legacy_events_get_synthesized_seq(client, monkeypatch):
    rid = _park(client, monkeypatch)
    p = _events_path(rid)
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    stripped = [json.dumps({k: v for k, v in json.loads(ln).items() if k != "seq"}, ensure_ascii=False) for ln in lines]
    p.write_text("\n".join(stripped) + "\n", encoding="utf-8")

    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    seqs = [e["seq"] for e in snap["events"]]
    n = len(snap["events"])
    assert n == len(lines)
    assert seqs == list(range(1, n + 1))
    assert snap["last_seq"] == n


def test_12_corrupt_line_skipped(client, monkeypatch):
    rid = _park(client, monkeypatch)
    p = _events_path(rid)
    before = L.evs(client, rid)
    with p.open("a", encoding="utf-8") as fh:
        fh.write("{{{{{ not json at all\n")
    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    assert L.kinds(snap["events"]) == L.kinds(before)
    assert [e["seq"] for e in snap["events"]] == [e["seq"] for e in before]


def test_13_empty_events_snapshot(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(1, name="WSEmpty"))
    L.patch_model(monkeypatch, [script_raise(RuntimeError("boom"))])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "failed"
    assert _events_path(rid).exists()
    _events_path(rid).write_text("", encoding="utf-8")

    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    assert snap["events"] == []
    assert snap["last_seq"] == 0
    assert snap["run"]["status"] == "failed"


# --------------------------------------------------------------------------- #
# 14-16: payload details + isolation
# --------------------------------------------------------------------------- #
def test_14_interrupt_event_carries_question(client, monkeypatch):
    rid = _park(client, monkeypatch, question="要继续吗？")
    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    interrupts = [e for e in snap["events"] if e["kind"] == "interrupt"]
    assert interrupts and interrupts[-1].get("question") == "要继续吗？"


def test_15_seq_order_total_across_kinds(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    seqs = [e["seq"] for e in snap["events"]]
    kinds = L.kinds(snap["events"])
    assert len(set(kinds)) > 1  # events of different kinds interleave in one order
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_16_two_runs_isolated_channels(client, monkeypatch):
    wf_a = L.make_wf(client, L.dsl_with_human())
    wf_b = L.make_wf(client, L.dsl_linear(2, name="WSIsoB"))
    L.patch_model(monkeypatch, ["one", "two"])
    rid_a = L.run_wf(client, wf_a["id"])
    rid_b = L.run_wf(client, wf_b["id"])
    assert L.await_run(client, rid_a)["status"] == "paused"
    assert L.await_run(client, rid_b)["status"] == "done"

    with _open(client, rid_a) as ws:
        _snap(client, ws, rid_a)
        L.resume(client, rid_a, {"answer": "go"})
        frames = L.ws_drain(ws, L.is_snapshot_with("done"))
    for f in frames:
        assert f.get("run_id") != rid_b, f
        assert (f.get("payload") or {}).get("run_id") != rid_b, f
        assert (f.get("run") or {}).get("id") != rid_b, f
    assert any(e["kind"] == "done" for e in _run_events(frames))


# --------------------------------------------------------------------------- #
# 17-20: reconnect + completeness
# --------------------------------------------------------------------------- #
def test_17_reconnect_replays_full_history(client, monkeypatch):
    rid = _park(client, monkeypatch)
    with _open(client, rid) as ws:
        first = _snap(client, ws, rid)
    parked_seq = first["last_seq"]

    r = L.resume(client, rid, {"answer": "go"})
    assert r.status_code == 200, r.text
    assert L.await_run(client, rid)["status"] == "done"

    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    kinds = L.kinds(snap["events"])
    assert "resume" in kinds and "done" in kinds
    seqs = [e["seq"] for e in snap["events"]]
    assert max(seqs) > parked_seq
    assert snap["last_seq"] == max(seqs) == max(seqs)


def test_18_snapshot_completeness_with_writes(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(2, writes_first=True, name="WSWrites"))
    L.patch_model(monkeypatch, [L.wj({"items": ["a", "b"]}), "ok"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"

    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)
    assert "s1__extract" in L.steps(snap["run"])
    assert "context_write" in L.kinds(snap["events"])


def test_19_seq_survives_counter_reset(client, monkeypatch):
    from ginno_runtime.workflows import events as wf_events

    rid = _park(client, monkeypatch)
    e1 = wf_events.append_event(rid, "note", text="before restart")
    wf_events._SEQ.pop(rid, None)  # simulated sidecar restart
    e2 = wf_events.append_event(rid, "note", text="after restart")
    stored = wf_events.read_events(rid)
    assert [e["seq"] for e in stored if e["kind"] == "note"][-2:] == [e1["seq"], e2["seq"]]
    assert e2["seq"] > e1["seq"]


def test_20_terminal_history_snapshot_immediate(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_linear(2, name="WSDone"))
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"

    with _open(client, rid) as ws:
        snap = _snap(client, ws, rid)  # FIRST frame already carries the terminal state
    assert snap["run"]["status"] == "done"
    assert "done" in L.kinds(snap["events"])
    assert snap["last_seq"] == max(e["seq"] for e in snap["events"])
