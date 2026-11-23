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


def test_mixed_callbacks_live_and_reloaded(app):
    page, port, sid = app
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid)
    send_frame(page, sid, "turn.start", turn_id="batch-turn", agent_id="dev", text=COMBINED)
    expect(page.get_by_text("Coalesced plan", exact=True)).to_be_visible()
    expect(page.get_by_text("Subagent result · Coalesced result", exact=True)).to_be_visible()
    expect(page.get_by_text("Worker return: BatchWorker", exact=True)).to_be_visible()
    send_frame(page, sid, "turn.start", turn_id="batch-tasks", agent_id="dev", task_events=TASKS)
    for task in TASKS:
        expect(page.get_by_text(task["title"], exact=True)).to_be_visible()

    save_messages(sid, [
        HumanMessage(content=COMBINED, id="returns", additional_kwargs={"ginno_subagent_result": "plan-pl_batch,s_batch,w_batch"}),
        AIMessage(content="Returned", id="returned"),
        HumanMessage(content="Two background task events", id="tasks", additional_kwargs={"ginno_task_events": TASKS}),
        AIMessage(content="Acknowledged", id="ack"),
    ])
    open_session(page, port, sid)
    expect(page.get_by_text("Coalesced plan", exact=True)).to_have_count(1)
    expect(page.get_by_text("Subagent result · Coalesced result", exact=True)).to_have_count(1)
    expect(page.get_by_text("Worker return: BatchWorker", exact=True)).to_have_count(1)
    for task in TASKS:
        expect(page.get_by_text(task["title"], exact=True)).to_have_count(1)
