"""WebSocket end-to-end: the basic chat happy path.

Drives the REAL LangGraph through the sidecar's WebSocket with a scripted fake
LLM, asserting the exact event protocol the Next.js UI consumes.
"""

from __future__ import annotations

import pytest

from conftest import event_names, events_of
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def test_plain_answer_streams_tokens_and_ends(create_session, ws_conv):
    model = script(text="Hello from Ginno.")  # single-turn: no tool calls
    sid = create_session([model])

    with ws_conv(sid) as conv:
        conv.invoke("hi there")
        events = conv.recv_until("message.end", "error")

    names = event_names(events)
    # A new session's first turn auto-titles, emitting session_title just before
    # turn.start; turn.start must still be present and lead the turn itself.
    assert names[0] in ("turn.start", "session_title")
    assert "turn.start" in names
    # the assistant's text arrives as one or more token.delta chunks
    deltas = "".join(e.get("content", "") for e in events_of(events, "token.delta"))
    assert "Hello from Ginno." in deltas
    # per-turn housekeeping events fire before message.end
    assert "todos.changed" in names
    assert "workflows.changed" in names
    assert "artifacts.changed" in names
    assert names[-1] == "message.end"
    assert "error" not in names


def test_turn_start_reports_active_agent_name(create_session, ws_conv):
    sid = create_session([script(text="ok")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        events = conv.recv_until("message.end", "error")
    turn = events_of(events, "turn.start")[0]
    assert turn["agent_id"] == "dev"
    assert turn["name"] == "Dev Agent"


def test_ping_pong(create_session, ws_conv):
    sid = create_session([script(text="ok")])
    with ws_conv(sid) as conv:
        conv.send({"type": "ping"})
        ev = conv.recv()
    assert ev["event"] == "pong"


def test_unknown_type_returns_error(create_session, ws_conv):
    sid = create_session([script(text="ok")])
    with ws_conv(sid) as conv:
        conv.send({"type": "bogus"})
        ev = conv.recv()
    assert ev["event"] == "error"
    assert "bogus" in ev["message"]


def test_invalid_json_returns_error(create_session, ws_conv):
    sid = create_session([script(text="ok")])
    with ws_conv(sid) as conv:
        conv.ws.send_text("not json{")
        ev = conv.recv()
    assert ev["event"] == "error"


def test_unknown_session_closes_with_error(ws_conv):
    with ws_conv("does-not-exist") as conv:
        ev = conv.recv()
    assert ev["event"] == "error"
    assert "unknown session" in ev["message"]
