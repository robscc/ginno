"""E2E: the per-turn trace UUID supplied by the client is echoed on turn.start
and stamped onto every subsequent event of that turn (so a UUID shown on the
bubble greps the sidecar logs)."""

from __future__ import annotations

import pytest

from conftest import events_of
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e

TURN = "11111111-2222-3333-4444-555555555555"


def test_client_turn_id_propagates_to_events(client, create_session, ws_conv, monkeypatch):
    from ginno_runtime import server
    from ginno_runtime.testing.fake_model import ScriptedChatModel

    sm = ScriptedChatModel(scripts=[script(text="hello there")])
    monkeypatch.setattr(server, "build_model", lambda *a, **k: sm)
    sid = create_session(sm, agent_id="dev")

    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "hi", "turn_id": TURN})
        events = conv.recv_until("message.end", "error")

    start = events_of(events, "turn.start")
    assert start, [e.get("event") for e in events]
    assert start[0].get("turn_id") == TURN
    # every event of the turn carries the same turn_id
    assert all(e.get("turn_id") == TURN for e in events), [
        (e.get("event"), e.get("turn_id")) for e in events
    ]
