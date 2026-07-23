"""E2E: the per-turn trace logger (ginno.turn) persists turn_start/turn_done to
~/.ginno/logs/sidecar.log, tagged with the client-supplied turn_id — so a UUID
copied off a chat bubble actually greps the log (the point of turn tracing)."""

from __future__ import annotations

import logging

import pytest

from ginno_runtime import server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.e2e

TURN = "trace-uuid-logcheck-7777"


def test_turn_trace_logged_to_file_with_turn_id(client, create_session, ws_conv, monkeypatch):
    sm = ScriptedChatModel(scripts=[script(text="traced reply")])
    monkeypatch.setattr(server, "build_model", lambda *a, **k: sm)
    sid = create_session(sm, agent_id="dev")

    with ws_conv(sid) as conv:
        conv.send({"type": "invoke", "message": "hello trace", "turn_id": TURN})
        conv.recv_until("message.end", "error")

    # flush any buffered log records before reading the file
    for h in server._log.handlers:
        h.flush()

    log_path = server.paths.home() / "logs" / "sidecar.log"
    assert log_path.exists(), "turn log file was not created"
    text = log_path.read_text(encoding="utf-8")
    assert TURN in text, f"turn_id not found in log:\n{text}"
    assert "turn_start" in text
    assert "turn_done" in text
