"""The main chat displays streamed tool arguments before the final preview."""

import pytest
from playwright.sync_api import expect
from test_packaged_ui_transcript_event_rows import (
    app as app,
    capture_socket,
    open_session,
    send_frame,
    wait_for_session_socket,
)

pytestmark = pytest.mark.e2e


def test_pending_tool_shows_streamed_args(app):
    page, port, sid = app
    capture_socket(page)
    open_session(page, port, sid)
    # A late idle turn.state reconciles from history and clears a synthetic turn.
    wait_for_session_socket(page, sid, require_turn_state=True)
    send_frame(page, sid, "turn.start", turn_id="args-turn", agent_id="dev")
    send_frame(page, sid, "tool.start", id="call-args", name="bash")
    send_frame(page, sid, "tool.args_delta", id="call-args", tail="audit argument snapshot one", total=28)
    expect(page.get_by_text("audit argument snapshot one", exact=False)).to_be_visible()
    send_frame(page, sid, "tool.args_delta", id="call-args", tail="audit argument snapshot two", total=30)
    expect(page.get_by_text("audit argument snapshot one", exact=False)).to_have_count(0)
    expect(page.get_by_text("audit argument snapshot two", exact=False)).to_be_visible()
    send_frame(page, sid, "tool.args", id="call-args", preview="audit final argument preview")
    expect(page.get_by_text("audit argument snapshot two", exact=False)).to_have_count(0)
    expect(page.get_by_text("audit final argument preview", exact=False)).to_be_visible()
