"""Background task and plan updates stay visible as independent transcript rows."""

import uuid
from datetime import datetime, timezone

import pytest
import test_packaged_ui_tasks_overview as harness
from langchain_core.messages import HumanMessage
from playwright.sync_api import expect, sync_playwright

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.compaction import _copy_with_new_id

pytestmark = pytest.mark.e2e


@pytest.fixture
def app(tmp_path):
    harness._require_runtime()
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port)
    try:
        harness._wait_health(port)
        session = harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Transcript event rows",
        })
        with sync_playwright() as pw:
            browser, page = harness._new_page(pw)
            try:
                yield page, port, session["id"]
            finally:
                browser.close()
    finally:
        harness._stop(proc, log)


def open_session(page, port, sid):
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    page.wait_for_function('typeof window.__ginnoOpenSession === "function"')
    page.evaluate("sid => window.__ginnoOpenSession(sid)", sid)


def save_messages(sid, messages):
    FileCheckpointer("default").put(
        {"configurable": {"thread_id": sid, "project_slug": "default"}},
        {
            "v": 4, "id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": 1}, "versions_seen": {},
            "updated_channels": ["messages"],
        },
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": 1},
    )


@pytest.mark.parametrize("legacy", [False, True], ids=["compacted-with-metadata", "legacy"])
def test_task_and_plan_after_reload(app, legacy):
    page, port, sid = app
    title = "Transcript build receipt"
    log_text = "transcript-build-log-sentinel"
    task = HumanMessage(
        id="task-turn", content=(f'[task finished] "{title}" (t_audit1) exit 0 · ran 1s\n'
                                  f"--- tail ---\n{log_text}\n--- end ---\nFull log: /tmp/build.log"),
        additional_kwargs={"ginno_task_event": {"task": "t_audit1", "kind": "finished", "title": title, "exit": 0}},
    )
    plan = HumanMessage(
        id="plan-turn", content='<ginno_plan_update plan="pl_audit">Plan receipt sentinel</ginno_plan_update>',
        additional_kwargs={"ginno_subagent_result": "plan-pl_audit"},
    )
    if legacy:
        # Existing checkpoints written before the fix already lack additional_kwargs.
        task = HumanMessage(content=task.content, id="keep_legacy_task")
        plan = HumanMessage(content=plan.content, id="keep_legacy_plan")
    else:
        task, plan = _copy_with_new_id(task), _copy_with_new_id(plan)
    save_messages(sid, [task, plan])
    open_session(page, port, sid)

    expect(page.get_by_text(title, exact=True)).to_be_visible(timeout=15_000)
    expect(page.get_by_text("Task finished · exit 0", exact=False)).to_have_count(1)
    expect(page.get_by_text("Plan status update", exact=True)).to_have_count(1)
    expect(page.get_by_text("Plan receipt sentinel", exact=True)).to_be_visible()
    expect(page.get_by_text(log_text, exact=False)).not_to_be_visible()
    if legacy:
        page.locator("details", has_text=log_text).locator("summary").click()
        expect(page.get_by_text(log_text, exact=False)).to_be_visible()


def capture_socket(page):
    page.add_init_script('''
        window.__transcriptTestSockets = [];
        const OriginalWebSocket = window.WebSocket;
        window.WebSocket = class extends OriginalWebSocket {
          constructor(...args) {
            super(...args);
            window.__transcriptTestSockets.push(this);
          }
        };
    ''')


def wait_for_session_socket(page, sid):
    page.wait_for_function('''sid => window.__transcriptTestSockets.some(ws =>
        new URL(ws.url).pathname.endsWith('/' + sid) && ws.readyState === WebSocket.OPEN)''', arg=sid)


def send_frame(page, sid, event, **payload):
    page.evaluate('''([sid, frame]) => {
        const ws = window.__transcriptTestSockets.find(ws =>
            new URL(ws.url).pathname.endsWith('/' + sid) && ws.readyState === WebSocket.OPEN);
        ws.dispatchEvent(new MessageEvent('message', {data: JSON.stringify(frame)}));
    }''', [sid, {"event": event, **payload}])


def test_independent_event_rows_from_stream(app):
    page, port, sid = app
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid)
    send_frame(page, sid, "steer.absorbed", steer_id="bg-fixture",
               task={"task": "t_live", "kind": "finished", "title": "Live background receipt", "exit": 0})
    send_frame(page, sid, "steer.absorbed", steer_id="plan-fixture",
               text='<ginno_plan_update plan="pl_live">Live system plan receipt</ginno_plan_update>')
    send_frame(page, sid, "turn.start", turn_id="turn-fixture", agent_id="dev",
               text='<ginno_plan_update plan="pl_wake">Live wake plan receipt</ginno_plan_update>')

    expect(page.get_by_text("Live background receipt", exact=True)).to_be_visible()
    expect(page.get_by_text("Live system plan receipt", exact=True)).to_be_visible()
    expect(page.get_by_text("Live wake plan receipt", exact=True)).to_be_visible()
    expect(page.get_by_text("Plan status update", exact=True)).to_have_count(2)
