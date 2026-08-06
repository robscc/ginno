"""WebSocket E2E: agent drives the TODO subsystem via todo_* tools."""

from __future__ import annotations

import pytest

from conftest import event_names, events_of
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def test_agent_todo_create_reflects_in_api(create_session, ws_conv, client):
    model = [
        script(tool_calls=[script_tool_call("todo_create", {"title": "Ship the thing", "priority": "high"})]),
        script(text="added it"),
    ]
    sid = create_session(model, agent_id="dev")  # dev has todo_* tools, no prompt needed
    with ws_conv(sid) as conv:
        conv.invoke("add a todo")
        events = conv.recv_until("message.end", "error")

    # the panel-refresh event fires, and the item is now in the store
    assert "todos.changed" in event_names(events)
    titles = [t["title"] for t in client.get("/api/todos").json()]
    assert "Ship the thing" in titles


def test_agent_todo_done(create_session, ws_conv, client):
    # seed one todo via the API, then have the agent mark it done
    todo = client.post("/api/todos", json={"title": "Finish report"}).json()["todo"]
    model = [
        script(tool_calls=[script_tool_call("todo_done", {"todo_id": todo["id"]})]),
        script(text="marked done"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("finish the report")
        conv.recv_until("message.end", "error")

    updated = next(t for t in client.get("/api/todos").json() if t["id"] == todo["id"])
    assert updated["done"] is True
    assert updated["completed_at"] is not None


def test_research_agent_read_only_todo(create_session, ws_conv, client):
    # research has only todo_list (read-only); a create attempt is blocked, not prompted
    model = [
        script(tool_calls=[script_tool_call("todo_create", {"title": "should not appear"})]),
        script(text="ok"),
    ]
    sid = create_session(model, agent_id="research")
    with ws_conv(sid) as conv:
        conv.invoke("add todo")
        events = conv.recv_until("message.end", "error")
    assert "permission.request" not in event_names(events)
    titles = [t["title"] for t in client.get("/api/todos").json()]
    assert "should not appear" not in titles


def test_todo_autolinks_session_when_touched(create_session, ws_conv, client):
    """A TODO created/completed inside a session is associated with it, so the
    panel can offer 'jump back to the session where this happened'."""
    model = [
        script(tool_calls=[script_tool_call("todo_create", {"title": "Linked item"})]),
        script(text="added"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("add a todo")
        conv.recv_until("message.end", "error")

    t = next(t for t in client.get("/api/todos").json() if t["title"] == "Linked item")
    assert sid in (t.get("session_ids") or [])


def test_todo_link_tool_attaches_artifact(create_session, ws_conv, client):
    model = [
        script(tool_calls=[script_tool_call("todo_create", {"title": "With artifact"})]),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("add a todo")
        conv.recv_until("message.end", "error")
    created = next(t for t in client.get("/api/todos").json() if t["title"] == "With artifact")

    model2 = [
        script(tool_calls=[script_tool_call("todo_link", {"todo_id": created["id"], "artifact_id": "art123"})]),
        script(text="linked"),
    ]
    sid2 = create_session(model2, agent_id="dev")
    with ws_conv(sid2) as conv:
        conv.invoke("link it")
        conv.recv_until("message.end", "error")

    t = next(t for t in client.get("/api/todos").json() if t["id"] == created["id"])
    assert "art123" in (t.get("artifact_ids") or [])
    # the linking session got associated too (the item was touched in sid2)
    assert sid2 in (t.get("session_ids") or [])
