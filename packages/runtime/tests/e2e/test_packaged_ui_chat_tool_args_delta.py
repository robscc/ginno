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
    turn_id = "args-turn"
    call = {"turn_id": turn_id, "id": "call-args"}
    send_frame(page, sid, "turn.start", turn_id=turn_id, agent_id="dev")
    send_frame(page, sid, "tool.start", **call, name="bash")
    send_frame(page, sid, "tool.args_delta", **call, tail="audit argument snapshot one", total=28)

    process = page.locator("[data-process-active]")
    toggle = process.locator(":scope > button")
    first = process.get_by_text("· audit argument snapshot one", exact=True)
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(first).not_to_be_visible()
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(first).to_be_visible()

    second = process.get_by_text("· audit argument snapshot two", exact=True)
    send_frame(page, sid, "tool.args_delta", **call, tail="audit argument snapshot two", total=30)
    expect(second).to_be_visible()
    expect(first).to_have_count(0)
    send_frame(page, sid, "tool.args", **call, preview="audit final argument preview")
    expect(process.get_by_text("· audit final argument preview", exact=True)).to_be_visible()
    expect(second).to_have_count(0)
