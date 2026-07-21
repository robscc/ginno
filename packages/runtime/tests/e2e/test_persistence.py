"""WebSocket E2E: conversation history persists across a simulated restart.

Sessions survive a runtime restart because the session list lives on disk
(per-slug index) and history is restored by the FileCheckpointer keyed on
thread_id. Clearing server._SESSIONS simulates the process going away.
"""

from __future__ import annotations

import pytest

from conftest import events_of
from ginno_runtime import server
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def _answer_text(events):
    return "".join(e.get("content", "") for e in events_of(events, "token.delta"))


def test_history_persists_across_restart(create_session, ws_conv):
    model = [script(text="first answer"), script(text="second answer")]
    sid = create_session(model, agent_id="dev")

    # turn 1
    with ws_conv(sid) as conv:
        conv.invoke("message one")
        ev1 = conv.recv_until("message.end", "error")
    assert "first answer" in _answer_text(ev1)

    # simulate a process restart: drop in-memory sessions; disk persists
    server._SESSIONS.clear()
    assert sid not in server._SESSIONS

    # turn 2 on the same session id -> graph rebuilt from disk, history restored
    with ws_conv(sid) as conv:
        conv.invoke("message two")
        ev2 = conv.recv_until("message.end", "error")
    assert "second answer" in _answer_text(ev2)

    # the checkpoint now holds both turns: human, ai, human, ai
    tup = FileCheckpointer(project_slug="default").get_tuple({"configurable": {"thread_id": sid}})
    msgs = tup.checkpoint["channel_values"]["messages"]
    assert len(msgs) == 4
    human = [m for m in msgs if getattr(m, "type", None) == "human"]
    assert {m.content for m in human} == {"message one", "message two"}


def test_checkpoint_file_written_per_session(create_session, ws_conv, isolated_home):
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")
    session_file = isolated_home / "projects" / "default" / "sessions" / f"{sid}.json"
    assert session_file.is_file()
