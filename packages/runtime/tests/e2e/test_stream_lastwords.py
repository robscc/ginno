"""Exercise the staged turn lifecycle with the real graph and snapshot store."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from ginno_runtime import lastwords, paths, server_shared
from ginno_runtime.api.stream import engine
from ginno_runtime.api.stream.turn import _run_stream
from ginno_runtime.session_meta import _session_meta_patch
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def _complete_turn(client, sid, prompt):
    session = server_shared._SESSIONS[sid]
    tid = uuid4().hex
    config = {"configurable": {
        "thread_id": sid, "project_slug": "default", "agent_id": session["agent_id"],
        "turn_id": tid, "user_text": prompt,
    }}
    # Wait for the exit stack as well as the answer. message.end precedes cleanup.
    client.portal.call(_run_stream, None, session["graph"], config, prompt, session)
    return tid


@pytest.mark.parametrize("checkpoint", ["start", "complete"])
def test_staged_turn_snapshot_lifecycle(client, create_session, isolated_home, monkeypatch, checkpoint):
    # TestClient reuses the Python process after the previous lifespan shuts down.
    monkeypatch.setattr(lastwords, "_SHUTTING_DOWN", False)
    settings_path = isolated_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["devswap"] = {"lastwords": {"mode": "auto", "min_effort": {"tool_calls": 1}}}
    settings_path.write_text(json.dumps(settings))
    reply = "Read the fixture."
    sid = create_session([
        script(tool_calls=[script_tool_call("read_file", {"path": "fixture.txt"})]),
        script(text=reply),
    ], title="Snapshot lifecycle fixture")
    (paths.session_files_dir("default", sid) / "fixture.txt").write_text("fixture")
    snapshot = lastwords._path(sid)
    writes = []
    write = lastwords._write_atomic

    def record(path, data):
        write(path, data)
        if path == snapshot:
            writes.append(json.loads(path.read_text()))

    # Observe the real writes and allow the first tool boundary to persist in
    # the same short test turn, independently of the production throttle.
    monkeypatch.setattr(lastwords, "_write_atomic", record)
    monkeypatch.setattr(lastwords, "TOOL_WRITE_MIN_INTERVAL_S", 0)
    assert not snapshot.exists()
    prompt = "Read fixture.txt"
    tid = _complete_turn(client, sid, prompt)
    history = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert any(block.get("text") == reply for row in history for block in row["blocks"])
    assert writes and all(snap["reason"] == "running" and snap["turn_id"] == tid for snap in writes)
    assert writes[-1]["user_request_head"] == prompt
    assert writes[-1]["running_tools"] == ["read_file"]
    if checkpoint == "start":
        assert [snap["effort"]["tool_calls"] for snap in writes] == [0, 1]
    else:
        assert lastwords._worth_restoring(writes[-1])
        assert not snapshot.exists(), "a completed turn left a running snapshot on disk"
        assert lastwords.consume_lastwords() == []


def test_completed_turn_notifies_linked_subscriber(client, create_session, monkeypatch):
    subscriber = create_session([], title="Subscriber")
    sid = create_session([script(text="Finished the linked turn.")], title="Linked turn")
    _session_meta_patch("default", sid, {"subscribers": [{"session_id": subscriber}]})
    observed = []
    push = engine._push_session_event

    async def record(target, event, payload, turn_id=None):
        if event == "linked.done":
            observed.append((target, payload, turn_id))
        await push(target, event, payload, turn_id)

    monkeypatch.setattr(engine, "_push_session_event", record)
    tid = _complete_turn(client, sid, "Finish the linked turn")
    assert observed == [(subscriber, {
        "session_id": sid, "text": "Finished the linked turn.",
        "origin": "interactive", "worker_status": "working",
    }, tid)]
