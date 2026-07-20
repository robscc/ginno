"""WebSocket E2E: permission ask -> interrupt -> resume (allow / deny).

write_file is "ask" in the seeded default policy, so scripting a write_file tool
call drives the graph into an interrupt(); the WS layer surfaces it as a
permission.request and waits for a permission_response to resume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import event_names, events_of
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def _ws(home):
    return str(Path(home) / "ws")


def test_permission_allow_executes_tool(create_session, ws_conv, isolated_home):
    ws = _ws(isolated_home)
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hello", "workspace": ws})]),
        script(text="Wrote it."),
    ]
    sid = create_session(model, workspace=ws)

    with ws_conv(sid) as conv:
        conv.invoke("please write a file")
        first = conv.recv_until("permission.request", "message.end", "error")
        names = event_names(first)
        # the turn pauses on a permission request (no message.end yet)
        assert "permission.request" in names
        assert "message.end" not in names
        perm = events_of(first, "permission.request")[0]
        assert perm["tool"] == "write_file"

        conv.respond_permission("allow")
        rest = conv.recv_until("message.end", "error")

    rest_names = event_names(rest)
    assert "tool.end" in rest_names  # the write_file result bubble
    assert "message.end" in rest_names
    assert (Path(ws) / "out.txt").read_text() == "hello"


def test_permission_deny_blocks_tool(create_session, ws_conv, isolated_home):
    ws = _ws(isolated_home)
    model = [
        script(tool_calls=[script_tool_call("write_file", {"path": "out.txt", "content": "hello", "workspace": ws})]),
        script(text="Understood, skipping the write."),
    ]
    sid = create_session(model, workspace=ws)

    with ws_conv(sid) as conv:
        conv.invoke("please write a file")
        conv.recv_until("permission.request")
        conv.respond_permission("deny")
        rest = conv.recv_until("message.end", "error")

    # denied tool resolves as a blocked tool.end and the file is never written
    tool_ends = events_of(rest, "tool.end")
    assert any("denied" in (e.get("content", "")).lower() for e in tool_ends)
    assert not (Path(ws) / "out.txt").exists()
    assert "message.end" in event_names(rest)


def test_allowed_tool_needs_no_permission(create_session, ws_conv, isolated_home):
    # read_file is in the default allow list -> runs without a permission prompt
    ws = _ws(isolated_home)
    Path(ws).mkdir(parents=True, exist_ok=True)
    (Path(ws) / "in.txt").write_text("payload")
    model = [
        script(tool_calls=[script_tool_call("read_file", {"path": "in.txt", "workspace": ws})]),
        script(text="got it"),
    ]
    sid = create_session(model, workspace=ws)
    with ws_conv(sid) as conv:
        conv.invoke("read it")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert "permission.request" not in names
    assert "tool.end" in names
    assert "message.end" in names
