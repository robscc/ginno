"""Coalesced callbacks retain all visible receipts in live chat and history."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from playwright.sync_api import expect
from test_packaged_ui_transcript_event_rows import (
    app as app,
    capture_socket,
    open_session,
    save_messages,
    send_frame,
    wait_for_session_socket,
)

pytestmark = pytest.mark.e2e

PLAN = '<ginno_plan_update plan="pl_batch">Coalesced plan</ginno_plan_update>'
RESULT = '<ginno_subagent_result session="s_batch" goal="Coalesced result">Result details</ginno_subagent_result>'
WORKER = '<ginno_worker_result session="w_batch" msg_id="w_final" from="BatchWorker" status="done">Final report</ginno_worker_result>'
COMBINED = f"Below are the returns from 3 subagents:\n\n{PLAN}\n\n{RESULT}\n\n{WORKER}"
TASKS = [
    {"task": "t_batch1", "kind": "finished", "title": "First background event", "exit": 0},
    {"task": "t_batch2", "kind": "timeout", "title": "Second background event"},
]


def expand_folds(transcript):
    """Open every 「执行过程」 group: report text sits behind the toggle
    (report fold, design A — 2026-11-25)."""
    for button in transcript.locator("[data-process-fold] > button[aria-expanded=false]").all():
        button.click()


def test_mixed_callbacks_live_and_reloaded(app):
    page, port, sid = app
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    transcript = page.locator("[data-chat-scroll]")
    callback_titles = ["Coalesced plan", "Subagent result · Coalesced result", "Worker return: BatchWorker"]
    send_frame(page, sid, "turn.start", turn_id="batch-turn", agent_id="dev", text=COMBINED)
    # The plan note and the subagent result fold into one group; the worker
    # return is not a report and stays a visible card.
    expect(transcript.get_by_text("Worker return: BatchWorker", exact=True)).to_be_visible()
    fold = transcript.locator("[data-process-fold] > button", has_text="subagent reports")
    expect(fold).to_contain_text("1 subagent reports")
    expect(transcript.get_by_text("Coalesced plan", exact=True)).not_to_be_visible()
    expand_folds(transcript)
    for title in callback_titles:
        expect(transcript.get_by_text(title, exact=True)).to_be_visible()
    send_frame(page, sid, "message.end", turn_id="batch-turn", origin="subagent")
    send_frame(page, sid, "turn.start", turn_id="batch-tasks", agent_id="dev", task_events=TASKS)
    for task in TASKS:
        expect(transcript.get_by_text(task["title"], exact=True)).to_be_visible()
    send_frame(page, sid, "message.end", turn_id="batch-tasks", origin="subagent")

    save_messages(sid, [
        HumanMessage(content=COMBINED, id="returns", additional_kwargs={"ginno_subagent_result": "plan-pl_batch,s_batch,w_batch"}),
        AIMessage(content="Returned", id="returned"),
        HumanMessage(content="Two background task events", id="tasks", additional_kwargs={"ginno_task_events": TASKS}),
        AIMessage(content="Acknowledged", id="ack"),
    ])
    open_session(page, port, sid)
    expect(transcript.get_by_text("Acknowledged", exact=True)).to_be_visible()
    # Same fold after reload: the envelopes lead the "Returned" bubble, where
    # they are detached and rendered exactly like the live row above.
    expect(transcript.locator("[data-process-fold] > button", has_text="subagent reports")).to_be_visible()
    expand_folds(transcript)
    for title in [*callback_titles, *(task["title"] for task in TASKS)]:
        entry = transcript.get_by_text(title, exact=True)
        expect(entry).to_have_count(1)
        expect(entry).to_be_visible()
