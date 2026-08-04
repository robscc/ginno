"""WebSocket E2E: per-turn agent routing and per-agent tools_allow enforcement."""

from __future__ import annotations


import pytest

from conftest import event_names, events_of
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def test_turn_start_reports_routed_agent(create_session, ws_conv):
    # session defaults to dev, but this turn explicitly routes to research
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello", agent_id="research")
        events = conv.recv_until("message.end", "error")
    turn = events_of(events, "turn.start")[0]
    assert turn["agent_id"] == "research"
    assert turn["name"] == "Research Agent"


def test_routing_updates_session_agent(create_session, ws_conv, client):
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello", agent_id="writer")
        conv.recv_until("message.end", "error")
    # the session's agent was switched server-side
    meta = client.get(f"/api/sessions/{sid}").json()
    assert meta["agent_id"] == "writer"


def test_agent_tools_allow_blocks_disallowed_tool(create_session, ws_conv, isolated_home):
    # research's tools_allow excludes write_file -> blocked before any permission prompt
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "x.txt", "content": "y"})]),
        script(text="sorry, I can't write."),
    ]
    sid = create_session(model, agent_id="research")
    with ws_conv(sid) as conv:
        conv.invoke("write something")
        events = conv.recv_until("message.end", "error")

    names = event_names(events)
    # blocked by policy enforcement, not by an interactive permission prompt
    assert "permission.request" not in names
    tool_ends = events_of(events, "tool.end")
    assert any("不可用" in (e.get("content", "")) for e in tool_ends)
    from ginno_runtime import paths

    assert not (paths.session_files_dir("default", sid) / "x.txt").exists()
    assert "message.end" in names


def test_dev_agent_can_use_write_with_permission(create_session, ws_conv, isolated_home):
    # dev has "*" tools -> write_file reaches the permission gate (ask), not a hard block
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "x.txt", "content": "y"})]),
        script(text="done"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("write")
        events = conv.recv_until("permission.request", "message.end", "error")
    assert "permission.request" in event_names(events)
