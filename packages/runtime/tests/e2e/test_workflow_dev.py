"""E2E test for P5 conversational editing on a SINGLE persistent WebSocket — the
real frontend pattern (one socket: receive version.propose, then send the
decision on the same socket). The workflow-dev agent calls workflow_propose_edit,
whose interrupt() pauses the graph; the server emits `version.propose`; the
client responds; on Allow a new immutable version is created. The confirmation
is independent of the permission system."""

from __future__ import annotations

import json
import queue
import threading

import pytest

from conftest import events_of
from ginno_runtime import workflows as wf_store
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

pytestmark = pytest.mark.e2e


def _add_step_dsl(cur_dsl: dict) -> dict:
    d = json.loads(json.dumps(cur_dsl))
    d["nodes"].append({"id": "s3", "type": "step", "agent": "dev", "goal": "new step"})
    d["edges"].append({"from": "s2", "to": "s3"})
    return d


def _drain(conv, q: queue.Queue, stop: threading.Event):
    while not stop.is_set():
        try:
            q.put(conv.recv())
        except Exception:
            return


def _wait(q: queue.Queue, *terminal, timeout: float = 20.0):
    evs = []
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ev = q.get(timeout=0.5)
        except queue.Empty:
            continue
        evs.append(ev)
        if ev.get("event") in terminal:
            return evs
    return evs


def _run_propose_flow(client, create_session, ws_conv, monkeypatch, decision: str):
    from ginno_runtime import server

    wf = wf_store.create_def(
        {
            "name": "WF",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "a"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "b"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    wid = wf["id"]
    new_dsl = _add_step_dsl(wf["dsl"])
    sm = ScriptedChatModel(
        scripts=[
            script(
                text="",
                tool_calls=[
                    script_tool_call(
                        "workflow_propose_edit",
                        {"workflow_id": wid, "new_dsl_json": json.dumps(new_dsl), "rationale": "add s3"},
                    )
                ],
            ),
            script(text="done"),
        ]
    )
    monkeypatch.setattr(server, "build_model", lambda *a, **k: sm)
    sid = create_session(sm, agent_id="workflow-dev")

    with ws_conv(sid) as conv:
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        t = threading.Thread(target=_drain, args=(conv, q, stop), daemon=True)
        t.start()
        try:
            conv.send({"type": "invoke", "message": "add a final step"})
            first = _wait(q, "version.propose", "error")
            assert events_of(first, "version.propose"), [e.get("event") for e in first]
            # graph must be paused: no terminal message.end before the decision
            assert "message.end" not in [e.get("event") for e in first]
            conv.send({"type": "permission_response", "decision": decision})
            tail = _wait(q, "message.end", "error")
            assert "error" not in [e.get("event") for e in tail], [e.get("event") for e in tail]
        finally:
            stop.set()
    return wid


def test_propose_edit_apply_creates_version(client, create_session, ws_conv, monkeypatch):
    wid = _run_propose_flow(client, create_session, ws_conv, monkeypatch, "allow")
    assert wf_store.get_def(wid)["version"] == 2
    assert len(wf_store.get_def(wid)["dsl"]["nodes"]) == 3


def test_propose_edit_reject_keeps_version(client, create_session, ws_conv, monkeypatch):
    wid = _run_propose_flow(client, create_session, ws_conv, monkeypatch, "deny")
    assert wf_store.get_def(wid)["version"] == 1
