"""E2E: the subagent P1 loop against the REAL server machinery.

Covers the seam the unit tests deliberately fake: scheduler spawn → a REAL
child turn through ``_run_stream`` (own checkpoint, own graph) → the completion
gate finalizes ``done`` → the result is injected into the idle parent as a NEW
turn whose entry HumanMessage carries the ``ginno_subagent_result`` tag, with
``subagent.spawned`` / ``subagent.status`` frames reaching the parent socket
(contracts 2/3/6).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from ginno_runtime import subagent_scheduler as sched
from ginno_runtime.testing.fake_model import (
    ScriptedChatModel,
    script,
    script_raise,
)

pytestmark = pytest.mark.e2e


def test_spawn_to_result_injection_full_loop(client, create_session, monkeypatch):
    # Script 1 = the child's final report (short → no compression call);
    # script 2 = the parent's reply to the injected result (the wake turn).
    model = ScriptedChatModel(
        scripts=[
            script(text="证据：测试输出。结论：任务完成。"),
            script(text="子代理结论：任务完成。"),
        ]
    )
    monkeypatch.setattr("ginno_runtime.api.sessions.build_model", lambda *a, **k: model)
    monkeypatch.setattr(sched, "_SPAWN_LOCK", asyncio.Lock())  # fresh loop binding
    parent = create_session(model, agent_id="dev")

    with client.websocket_connect(f"/api/ws/sessions/{parent}") as ws:
        res = client.portal.call(
            sched.create_subagent, parent, "独立小任务", "只读", "有结论"
        )
        assert res["ok"] and res["depth"] == 0
        child = res["session_id"]

        # Drain the parent socket until the wake turn finishes (or errors —
        # an error must also end the wait, or a failure hangs the test).
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev.get("event") in ("message.end", "error"):
                break
        names = [e.get("event") for e in events]
        assert not events_of_local(events, "error"), events
        # contract 2: spawned + terminal status reach the parent's sockets
        assert "subagent.spawned" in names
        statuses = [e for e in events if e.get("event") == "subagent.status"]
        assert statuses and statuses[-1]["session_id"] == child
        assert statuses[-1]["status"] == "done"
        assert statuses[-1]["parent_session_id"] == parent
        # the injection opened a REAL new turn on the idle parent (§5.5)
        assert "turn.start" in names

    # child meta finalized (contract 1: status + result_summary backfilled)
    child_meta = client.get(f"/api/sessions/{child}").json()
    assert child_meta["type"] == "subagent"
    assert child_meta["parent_session_id"] == parent
    assert child_meta["depth"] == 0
    assert child_meta["subagent"]["status"] == "done"
    assert child_meta["subagent"]["result_summary"].startswith("证据")
    assert child_meta["subagent"]["goal"] == "独立小任务"

    # contract 3: the parent transcript carries the tagged injection message
    # (the bare quotes are json-escaped inside the dumped blob)
    hist = client.get(f"/api/sessions/{parent}/history").json()["messages"]
    blob = json.dumps(hist, ensure_ascii=False)
    assert "ginno_subagent_result session=" in blob
    assert child in blob and "独立小任务" in blob
    # ...and the parent's scripted reply landed after it (the wake turn ran)
    assert "子代理结论" in blob


def events_of_local(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


def test_retry_of_abandoned_failed_subagent_stays_failed(
    client, create_session, monkeypatch
):
    """A checkpoint retry of an ABANDONED failed subagent turn runs under a
    DERIVED exec turn id, but the failure is persisted under the original
    client-facing id (``last_error.turn_id`` = ``_ui_turn_id``). The settle
    gate must key on the ui id: passing the derived id misses the record, and
    a permanently failed retry settles "done" — a phantom summary is injected
    into the parent while the UI shows the error card."""
    boom = ValueError("permanent provider failure")
    model = ScriptedChatModel(scripts=[script_raise(boom)] * 8)
    monkeypatch.setattr("ginno_runtime.api.sessions.build_model", lambda *a, **k: model)
    monkeypatch.setattr(sched, "_SPAWN_LOCK", asyncio.Lock())  # fresh loop binding
    parent = create_session(model, agent_id="dev")

    with client.websocket_connect(f"/api/ws/sessions/{parent}") as ws:
        res = client.portal.call(sched.create_subagent, parent, "会失败的任务")
        assert res["ok"]
        child = res["session_id"]
        # The first (headless) child turn fails permanently: the parent's socket
        # sees the failed finalize (§5.7 failure reporting).
        while True:
            ev = ws.receive_json()
            if ev.get("event") == "subagent.status":
                break
        assert ev["session_id"] == child and ev["status"] == "failed"

    from ginno_runtime.checkpointer import ABANDONED_TURNS
    from ginno_runtime.session_meta import _find_meta

    # Read the meta from disk (the gate's own source of truth): the HTTP GET
    # round-trips the in-memory entry INCLUDING the model object, whose
    # scripted raise-markers are not JSON-serializable.
    found = _find_meta(child)
    assert found
    ui_turn_id = ((found[0].get("last_error") or {}).get("turn_id")) or ""
    assert ui_turn_id
    # What the stall-watchdog / stop path leaves behind: the original id is
    # abandoned, so the retry handler derives "…--r<hex>" for checkpoint writes.
    ABANDONED_TURNS.add(ui_turn_id)

    with client.websocket_connect(f"/api/ws/sessions/{child}") as cws:
        cws.send_json({"type": "retry_from_checkpoint", "turn_id": ui_turn_id})
        while True:
            ev = cws.receive_json()
            if ev.get("event") in ("error", "message.end"):
                break
        assert ev.get("event") == "error"  # the retry failed too

        # The settle gate runs as a background task — poll for it to land.
        deadline = time.time() + 10.0
        status = ""
        while time.time() < deadline:
            f2 = _find_meta(child)
            status = ((f2[0].get("subagent") or {}).get("status")) if f2 else ""
            if status in ("done", "failed", "stopped"):
                break
            time.sleep(0.1)
        # Pre-fix this read "done": the gate was handed the derived exec id,
        # missed last_error (keyed on the ui id) and treated the failed retry
        # as a success.
        assert status == "failed", status
