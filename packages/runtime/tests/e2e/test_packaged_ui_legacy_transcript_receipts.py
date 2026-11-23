"""Legacy recovery and task callbacks replay as readable rows."""

import pytest
from langchain_core.messages import HumanMessage
from playwright.sync_api import expect
from test_packaged_ui_transcript_event_rows import app as app, open_session, save_messages

pytestmark = pytest.mark.e2e


def test_legacy_lastwords_and_timeout(app):
    page, port, sid = app
    save_messages(sid, [
        HumanMessage(
            id="keep_lastwords_legacy",
            content="[lastwords] Legacy recovery header\n\n原始请求：Legacy request detail\n最后进展：Legacy progress detail",
        ),
        HumanMessage(
            id="keep_timeout_legacy",
            content='[task timeout] "Legacy timeout" (t_audit9) after 20s — killed\n'
                    "--- tail ---\nLegacy timeout log\n--- end ---\nFull log: /tmp/task-output",
        ),
    ])
    open_session(page, port, sid)
    expect(page.get_by_text("Legacy recovery header", exact=True)).to_be_visible()
    expect(page.get_by_text("Legacy request detail", exact=True)).to_be_visible()
    expect(page.get_by_text("Legacy progress detail", exact=True)).to_be_visible()
    expect(page.get_by_text("Legacy timeout", exact=True)).to_be_visible()
    expect(page.get_by_text("Task timed out", exact=False)).to_have_count(1)
    expect(page.get_by_text("Legacy timeout log", exact=False)).not_to_be_visible()
    page.locator("details", has_text="Legacy timeout log").locator("summary").click()
    expect(page.get_by_text("Legacy timeout log", exact=False)).to_be_visible()


def test_legacy_batch_recovers_receipts_before_last_marked_event(app):
    page, port, sid = app
    save_messages(sid, [HumanMessage(
        id="legacy-batch",
        content='2 background task events arrived:\n\n'
                '[task finished] "Legacy batch first" (t_audit1) exit 0 · ran 1s\n\n'
                '[task timeout] "Legacy batch second" (t_audit2) after 3s — killed',
        additional_kwargs={"ginno_task_event": {
            "task": "t_audit2", "kind": "timeout", "title": "Legacy batch second", "dur": "3s",
        }},
    )])
    open_session(page, port, sid)
    expect(page.get_by_text("Legacy batch first", exact=True)).to_have_count(1)
    expect(page.get_by_text("Legacy batch second", exact=True)).to_have_count(1)
    expect(page.get_by_text("2 background task events arrived", exact=False)).to_have_count(0)
