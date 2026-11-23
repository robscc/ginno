"""Distinct Worker messages from one session remain visible across delivery paths."""

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


def test_worker_terminal_is_distinct_from_digest(app):
    page, port, sid = app
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid)
    digest = ('<ginno_worker_result session="worker-fixture" msg_id="msg-digest" from="AuditWorker" '
              'status="working" kind="digest">digest body</ginno_worker_result>')
    terminal = ('<ginno_worker_result session="worker-fixture" msg_id="msg-terminal" from="AuditWorker" '
                'status="done">terminal body</ginno_worker_result>')
    send_frame(page, sid, "steer.absorbed", steer_id="dedup-digest", text=digest)
    expect(page.get_by_text("Worker progress: AuditWorker", exact=True)).to_have_count(1)
    send_frame(page, sid, "turn.start", turn_id="worker-wake", agent_id="dev", text=terminal)
    send_frame(page, sid, "steer.absorbed", steer_id="dedup-terminal", text=terminal)
    send_frame(page, sid, "steer.absorbed", steer_id="dedup-digest-replay", text=digest)
    expect(page.get_by_text("Worker return: AuditWorker", exact=True)).to_have_count(1)
    expect(page.get_by_text("Worker progress: AuditWorker", exact=True)).to_have_count(1)
