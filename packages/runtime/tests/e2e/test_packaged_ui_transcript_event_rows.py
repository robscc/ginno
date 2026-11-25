"""Background task and plan updates stay visible as independent transcript rows.

Since 2026-11-25 (report fold, design A) a plan note is no longer a full card:
it folds into a 「执行过程」 group as one header line, with its text behind the
toggle. Background task bands are unchanged.
"""

import uuid
from datetime import datetime, timezone

import pytest
import test_packaged_ui_tasks_overview as harness
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from playwright.sync_api import expect, sync_playwright

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.compaction import _copy_with_new_id
from ginno_runtime.microcompact import CLEARED_TOOL_OUTPUT

pytestmark = pytest.mark.e2e


@pytest.fixture
def app(tmp_path, monkeypatch, request):
    language = getattr(request, "param", "en")
    settings = harness._settings()
    monkeypatch.setattr(harness, "_settings", lambda: {**settings, "language": language})
    harness._require_runtime()
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port)
    try:
        harness._wait_health(port)
        session = harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Transcript event rows",
        })
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.add_init_script(f"window.localStorage.setItem('ginno.language', '{language}')")
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


def test_mid_turn_compaction_renders_as_short_notice(app):
    page, port, sid = app
    summary_body = "summary-body-sentinel" * 9_000
    save_messages(sid, [
        HumanMessage(id="compact", content=f"[compact] 前段对话已压缩摘要（turn 中自动压缩）：\n{summary_body}"),
        HumanMessage(id="next-user", content="Continue after compaction"),
    ])
    open_session(page, port, sid)

    expect(page.get_by_text("Earlier conversation compacted into a summary")).to_be_visible(timeout=15_000)
    expect(page.get_by_text("summary-body-sentinel", exact=False)).to_have_count(0)
    expect(page.get_by_text("Continue after compaction", exact=True)).to_be_visible()


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
    fold = page.locator("[data-process-fold] > button")
    expect(fold).to_have_count(1)
    expect(fold).to_contain_text("1 plan updates")
    expect(page.get_by_text("Plan receipt sentinel", exact=True)).not_to_be_visible()
    fold.click()
    expect(page.get_by_text("Plan receipt sentinel", exact=True)).to_be_visible()
    expect(page.get_by_text("Plan status update", exact=True)).to_have_count(0)
    expect(page.get_by_text(log_text, exact=False)).not_to_be_visible()
    if legacy:
        page.locator("details", has_text=log_text).locator("summary").click()
        expect(page.get_by_text(log_text, exact=False)).to_be_visible()


def capture_socket(page):
    page.add_init_script('''
        window.__transcriptTestSockets = [];
        window.__transcriptProbedSockets = new WeakSet();
        const OriginalWebSocket = window.WebSocket;
        window.WebSocket = class extends OriginalWebSocket {
          constructor(...args) {
            super(...args);
            window.__transcriptTestSockets.push(this);
            this.addEventListener('message', event => {
              if (event.isTrusted && JSON.parse(event.data).event === 'turn.state') {
                window.__transcriptProbedSockets.add(this);
              }
            });
          }
        };
    ''')


def wait_for_session_socket(page, sid, *, require_turn_state=False):
    page.wait_for_function('''([sid, requireTurnState]) => window.__transcriptTestSockets.some(ws =>
        new URL(ws.url).pathname.endsWith('/' + sid) && ws.readyState === WebSocket.OPEN &&
        (!requireTurnState || window.__transcriptProbedSockets.has(ws)))''',
        arg=[sid, require_turn_state],
    )


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
    fold = page.locator("[data-process-fold] > button")
    expect(fold).to_have_count(1)
    expect(fold).to_contain_text("2 plan updates")
    expect(page.get_by_text("Live system plan receipt", exact=True)).not_to_be_visible()
    fold.click()
    expect(page.get_by_text("Live system plan receipt", exact=True)).to_be_visible()
    expect(page.get_by_text("Live wake plan receipt", exact=True)).to_be_visible()
    expect(page.get_by_text("Plan status update", exact=True)).to_have_count(0)


def dismiss_notice(notice):
    notice.hover()
    notice.get_by_role("button", name="Dismiss", exact=True).click(timeout=5_000)
    expect(notice).to_have_count(0)


def test_dismissed_context_notice_stays_closed_during_live_updates(app):
    page, port, sid = app
    save_messages(sid, [HumanMessage(id="initial", content="Context dismissal test")])
    capture_socket(page)
    open_session(page, port, sid)
    expect(page.get_by_text("Context dismissal test", exact=True)).to_be_visible()
    wait_for_session_socket(page, sid, require_turn_state=True)

    send_frame(page, sid, "context.microcompacted", turn_id="cleanup-1", cleared_tool_outputs=82)
    cleanup = page.get_by_text("Cleared 82 older tool outputs", exact=False)
    dismiss_notice(cleanup)

    send_frame(page, sid, "context.compacted", turn_id="cleanup-1", compacted_messages=320)
    summary = page.get_by_text("Conversation compacted: 320 older messages", exact=False)
    expect(summary).to_be_visible()
    expect(cleanup).to_have_count(0)

    # The same wording can belong to a new event in this session.
    send_frame(page, sid, "context.microcompacted", turn_id="cleanup-2", cleared_tool_outputs=82)
    expect(cleanup).to_be_visible()
    expect(cleanup).to_have_count(1)
    expect(summary).to_be_visible()

    # Closing a chip must preserve its siblings, including their original indexes.
    send_frame(page, sid, "context.updated", changes=[
        {"section": "files", "summary": "First context change"},
        {"section": "files", "summary": "Second context change"},
    ])
    first = page.get_by_text("First context change", exact=False)
    second = page.get_by_text("Second context change", exact=False)
    dismiss_notice(first)
    expect(second).to_be_visible()
    dismiss_notice(second)
    expect(first).to_have_count(0)


@pytest.mark.parametrize("from_live", [False, True], ids=["history", "live-to-history"])
def test_dismissed_context_notice_stays_closed_after_history_reconciliation(app, from_live):
    page, port, sid = app
    saved = [
        HumanMessage(id="initial", content="Reconcile the transcript"),
        AIMessage(id="tool-request", content="", tool_calls=[{"name": "bash", "args": {}, "id": "call-1"}]),
        ToolMessage(id="tool-result", name="bash", tool_call_id="call-1", content=CLEARED_TOOL_OUTPUT),
        AIMessage(id="reply", content="Saved reply before reconciliation"),
    ]
    save_messages(sid, saved[:1] if from_live else saved)
    capture_socket(page)
    open_session(page, port, sid)
    initial_text = "Reconcile the transcript" if from_live else "Saved reply before reconciliation"
    expect(page.get_by_text(initial_text, exact=True)).to_be_visible()
    wait_for_session_socket(page, sid, require_turn_state=True)
    if from_live:
        save_messages(sid, saved)
        history = harness._api(port, "GET", f"/api/sessions/{sid}/history")
        notice_id = history["messages"][0]["id"]
        send_frame(page, sid, "context.microcompacted", cleared_tool_outputs=1, notice_id=notice_id)
    cleanup = page.get_by_text("Cleared 1 older tool outputs", exact=False)
    dismiss_notice(cleanup)

    # A probe after a missed final frame replaces the whole view from /history.
    send_frame(page, sid, "turn.start", turn_id="probe-turn", agent_id="dev")
    save_messages(sid, [*saved, HumanMessage(id="new-history", content="Restored by history reconciliation")])
    with page.expect_response(lambda response: response.url.endswith(f"/api/sessions/{sid}/history")) as restored:
        send_frame(page, sid, "turn.state", running=False)
    assert restored.value.ok
    expect(page.get_by_text("Restored by history reconciliation", exact=True)).to_be_visible()
    expect(cleanup).to_have_count(0)
    notice = restored.value.json()["messages"][0]
    send_frame(page, sid, "context.microcompacted", cleared_tool_outputs=1, notice_id=notice["id"])
    send_frame(page, sid, "context.compacted", compacted_messages=5)
    expect(page.get_by_text("Conversation compacted: 5 older messages", exact=False)).to_be_visible()
    expect(cleanup).to_have_count(0)


def find_in_session(page, query):
    page.evaluate('window.dispatchEvent(new Event("ginno:find-open"))')
    field = page.locator('input[placeholder*="Find in session"], input[placeholder*="会话内搜索"]')
    expect(field).to_be_focused()
    field.fill(query)
    return field


def expect_active_find(page, text, *, row=None, offset=None, within=None):
    failure = page.evaluate('''async ({text, row, offset, within}) => {
        // Read the page's own CSS highlight registry in its JavaScript realm.
        const until = performance.now() + 5000;
        let observed;
        do {
            observed = [...(CSS.highlights.get('find-hit-active') ?? [])].map(range => {
                const node = range.startContainer.parentElement;
                const bounds = range.getBoundingClientRect();
                return {
                    text: range.toString(), offset: range.startOffset,
                    row: node?.closest('[data-msg-id]')?.dataset.msgId,
                    inside: !within || !!node?.closest(within),
                    visible: bounds.height > 0 && bounds.top >= 0 && bounds.bottom <= window.innerHeight,
                };
            });
            const match = observed[0];
            if (observed.length === 1 && match.text === text && match.visible && match.inside &&
                (!row || match.row === row) && (offset < 0 || match.offset === offset)) return null;
            await new Promise(requestAnimationFrame);
        } while (performance.now() < until);
        return observed;
    }''', {"text": text, "row": row or "", "offset": -1 if offset is None else offset, "within": within or ""})
    assert failure is None, f"Expected one active range {text!r} in row {row!r}, got {failure}"


@pytest.mark.parametrize("app,notice_text,dismiss_label,no_hits", [
    ("en", "Cleared 82 older tool outputs", "Dismiss", "No hits"),
    ("zh-CN", "已清理 82 条较早的工具输出", "关闭", "无命中"),
], indirect=["app"])
def test_find_visible_context_notice(app, notice_text, dismiss_label, no_hits):
    page, port, sid = app
    save_messages(sid, [HumanMessage(id="initial", content="Find context notice fixture")])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    expect(page.get_by_text("Find context notice fixture", exact=True)).to_be_visible()
    send_frame(page, sid, "context.microcompacted", turn_id="find-cleanup", cleared_tool_outputs=82)
    notice = page.get_by_text(notice_text, exact=False)
    expect(notice).to_be_visible()
    find_in_session(page, notice_text)
    expect(page.get_by_text("1/1", exact=True)).to_be_visible(timeout=5_000)
    expect_active_find(page, notice_text)
    notice.hover()
    notice.get_by_role("button", name=dismiss_label, exact=True).click()
    expect(notice).to_have_count(0)
    expect(page.get_by_text(no_hits, exact=True)).to_be_visible()
    assert page.evaluate("CSS.highlights.get('find-hit-active').size") == 0


def test_find_relocated_plan_and_report(app):
    page, port, sid = app
    plan = '<ginno_plan_update plan="pl_find">Folded plan search needle</ginno_plan_update>'
    report = ('<ginno_subagent_result session="synthetic-child" goal="Synthetic child">'
              'A result with folded **report** search needle.</ginno_subagent_result>')
    save_messages(sid, [
        HumanMessage(id="before", content="Start synthetic turn"),
        AIMessage(id="report-host", content="Previous response"),
        HumanMessage(content=plan, additional_kwargs={"ginno_subagent_result": "plan-pl_find"}),
        HumanMessage(content=report, additional_kwargs={"ginno_subagent_result": "synthetic-child"}),
        AIMessage(id="next-reply", content="Following response"),
    ])
    open_session(page, port, sid)
    body = page.get_by_text("Previous response", exact=True)
    expect(body).to_be_visible()
    host = page.locator('[data-msg-id="report-host"]')
    group = host.locator('[data-process-fold] > button')
    expect(group).to_have_attribute("aria-expanded", "false")
    field = find_in_session(page, "Folded plan search needle")
    expect(page.get_by_text("1/1", exact=True)).to_be_visible(timeout=5_000)
    expect_active_find(page, "Folded plan search needle", row="report-host", within='[data-testid="plan-update-row"]')
    expect(group).to_have_attribute("aria-expanded", "true")
    group.click()
    field.fill("Plan update")
    expect_active_find(page, "Plan update", row="report-host", within='[data-testid="plan-update-row"]')
    expect(group).to_have_attribute("aria-expanded", "true")
    group.click()
    field.fill("folded report search needle")
    expect_active_find(page, "folded report search needle", row="report-host", within="[data-report-body]")
    expect(group).to_have_attribute("aria-expanded", "true")
    expect(host.locator('[data-testid="subagent-result-card"] > button').first).to_have_attribute("aria-expanded", "true")
    expect(field).to_be_focused()


def test_find_preserves_selected_occurrence_after_unrelated_frames(app):
    page, port, sid = app
    save_messages(sid, [
        HumanMessage(id="one", content="ordinary needle"),
        AIMessage(id="two", content="ordinary needle"),
        HumanMessage(id="repeat", content="repeat needle then repeat needle"),
    ])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    expect(page.get_by_text("repeat needle then repeat needle", exact=True)).to_be_visible()
    field = find_in_session(page, "ordinary needle")
    expect(page.get_by_text("1/2", exact=True)).to_be_visible()
    expect_active_find(page, "ordinary needle", row="one")
    field.press("Enter")
    expect_active_find(page, "ordinary needle", row="two")
    send_frame(page, sid, "turn.start", turn_id="find-unrelated", agent_id="dev")
    send_frame(page, sid, "token.delta", content="Unrelated token output")
    expect(page.get_by_text("Unrelated token output", exact=True)).to_be_visible()
    expect(page.get_by_text("2/2", exact=True)).to_be_visible(timeout=5_000)
    expect_active_find(page, "ordinary needle", row="two")
    field.fill("repeat needle")
    expect(page.get_by_text("1/2", exact=True)).to_be_visible()
    expect_active_find(page, "repeat needle", row="repeat", offset=0)
    field.press("Enter")
    expect(page.get_by_text("2/2", exact=True)).to_be_visible()
    expect_active_find(page, "repeat needle", row="repeat", offset=len("repeat needle then "))
    field.press("Shift+Enter")
    expect_active_find(page, "repeat needle", row="repeat", offset=0)
    field.press("Escape")
    expect(field).to_have_count(0)


def test_find_selected_plan_survives_history_reconciliation(app):
    page, port, sid = app
    initial = HumanMessage(id="start", content="Find against live and replay")
    plans = [
        f'<ginno_plan_update plan="pl_find_{number}">Shared plan needle · entry {number}</ginno_plan_update>'
        for number in (1, 2)
    ]
    save_messages(sid, [initial, AIMessage(id="live-host", content="Initial report host")])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    expect(page.get_by_text("Initial report host", exact=True)).to_be_visible()
    for number, plan in enumerate(plans, start=1):
        send_frame(page, sid, "steer.absorbed", steer_id=f"find-plan-{number}", text=plan)
    expect(page.locator('[data-msg-id="live-host"] [data-process-fold] > button')).to_contain_text("2 plan updates")
    field = find_in_session(page, "Shared plan needle")
    expect(page.get_by_text("1/2", exact=True)).to_be_visible()
    field.press("Enter")
    expect(page.get_by_text("2/2", exact=True)).to_be_visible()
    # The marker is on the plan's text span. Inside the matching span, only
    # occurrence two has the second entry's title.
    second_plan = '[title="Shared plan needle · entry 2"]'
    expect_active_find(page, "Shared plan needle", row="live-host", within=second_plan)

    send_frame(page, sid, "turn.start", turn_id="find-reconcile", agent_id="dev")
    save_messages(sid, [
        initial,
        AIMessage(id="checkpoint-host", content="Reconciled report host"),
        *[HumanMessage(content=plan, additional_kwargs={"ginno_subagent_result": f"plan-pl_find_{number}"})
          for number, plan in enumerate(plans, start=1)],
        AIMessage(id="next-reconcile", content="After the replayed plan updates"),
    ])
    with page.expect_response(lambda response: response.url.endswith(f"/api/sessions/{sid}/history")) as restored:
        send_frame(page, sid, "turn.state", running=False)
    assert restored.value.ok
    expect(page.get_by_text("Reconciled report host", exact=True)).to_be_visible()
    expect(page.get_by_text("2/2", exact=True)).to_be_visible()
    expect_active_find(page, "Shared plan needle", row="checkpoint-host", within=second_plan)
    expect(field).to_be_focused()
