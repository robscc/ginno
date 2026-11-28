"""Exercise the staged turn lifecycle with the real graph and snapshot store."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from ginno_runtime import lastwords, paths, server_shared
from ginno_runtime.api.stream import engine
from ginno_runtime.api.stream.stages import dispatch_stream_graph
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
    assert writes.pop()["terminal"] == "completed"
    assert writes and all(snap["reason"] == "running" and snap["turn_id"] == tid for snap in writes)
    assert writes[0]["user_request_head"] == prompt
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


def test_opening_snapshot_uses_current_request_before_first_checkpoint(client, create_session, monkeypatch):
    monkeypatch.setattr(lastwords, "_SHUTTING_DOWN", False)
    sid = create_session([script(text="Previous response"), script(text="Current response")])
    _complete_turn(client, sid, "Previous request")
    writes = []
    original = lastwords._write_atomic

    def record(path, data):
        original(path, data)
        if path == lastwords._path(sid):
            writes.append(dict(data))

    monkeypatch.setattr(lastwords, "_write_atomic", record)
    tid = _complete_turn(client, sid, "Current request")
    assert writes[0]["turn_id"] == tid
    assert writes[0]["user_request_head"] == "Current request"
    assert writes[0]["last_agent_message"] == ""
    assert not lastwords._path(sid).exists()


async def test_unexpected_cancel_keeps_snapshot_but_stop_is_terminal(monkeypatch):
    monkeypatch.setattr(lastwords, "_SHUTTING_DOWN", False)
    entered = asyncio.Event()

    async def agent(_state):
        entered.set()
        await asyncio.Future()

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    graph = builder.compile()
    sid = "cancel-fixture"
    first, second = uuid4().hex, uuid4().hex
    config = {"configurable": {"thread_id": sid, "project_slug": "default", "turn_id": first}}
    state = {"messages": [HumanMessage(content="Current request", id=first)]}
    run = asyncio.create_task(dispatch_stream_graph(graph, config, state))
    await asyncio.wait_for(entered.wait(), 3)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert json.loads(lastwords._path(sid).read_text())["turn_id"] == first

    entered.clear()
    config["configurable"]["turn_id"] = second
    state["messages"] = [HumanMessage(content="Stop request", id=second)]
    run = asyncio.create_task(dispatch_stream_graph(graph, config, state))
    await asyncio.wait_for(entered.wait(), 3)
    server_shared._TURN_STOP[sid].set()
    assert await asyncio.wait_for(run, 3) == "stopped"
    assert not lastwords._path(sid).exists()


def test_startup_recovery_leaves_existing_question_answerable(
    client, create_session, ws_conv, isolated_home, monkeypatch,
):
    monkeypatch.setattr(lastwords, "_SHUTTING_DOWN", False)
    ask = script_tool_call("ask_user", {"question": "Choose a fixture", "options": ["A", "B"]}, "ask-lw")
    sid = create_session([script(tool_calls=[ask]), script(text="Original answer completed")])
    settings_path = isolated_home / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["devswap"] = {"lastwords": {"mode": "auto", "min_effort": {"tool_calls": 1}}}
    settings_path.write_text(json.dumps(settings))
    graph = server_shared._SESSIONS[sid]["graph"]
    config = {"configurable": {"thread_id": sid}}

    with ws_conv(sid) as conv:
        conv.invoke("Ask the question")
        question = conv.recv_until("user.question", "error")[-1]
    assert question["event"] == "user.question"
    snap = json.loads(lastwords._path(sid).read_text())
    snap["reason"] = "dev-swap"
    snap["effort"]["tool_calls"] = 1
    lastwords._write_atomic(lastwords._path(sid), snap)
    assert client.portal.call(lastwords.recover_lastwords) == 0
    assert lastwords.list_pending() == []
    assert lastwords.list_receipts()[0]["status"] == "awaiting_input"
    assert graph.checkpointer.get_pending_interrupt(config)[0].value["qid"] == question["qid"]
    with ws_conv(sid) as conv:
        reemitted = conv.recv_until("user.question", "error")[-1]
        assert reemitted["qid"] == question["qid"]
        conv.respond_answer("A", option_index=0)
        frames = conv.recv_until("runtime.lastwords_restored", "error")
        assert frames[-1]["status"] == "completed"
    assert graph.checkpointer.get_pending_interrupt(config) is None
