"""Background task and plan updates stay visible as independent transcript rows.

Since 2026-11-25 (report fold, design A) a plan note is no longer a full card:
it folds into a 「执行过程」 group as one header line, with its text behind the
toggle. Background task bands are unchanged.
"""

import json
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


@pytest.mark.parametrize(("app", "missing", "disconnected"), [
    ("en", "The file could not be found at its original location", "Could not connect to the Ginno runtime"),
    ("zh-CN", "原路径找不到文件", "无法连接 Ginno 运行时"),
], indirect=["app"])
def test_file_reference_shows_open_failure_and_can_retry(app, missing, disconnected, tmp_path):
    page, port, sid = app
    path = tmp_path.resolve() / "previously-attached-report.md"
    name = "Preview fixture report"
    save_messages(sid, [
        HumanMessage(id="file-reference-request", content="Please attach the report"),
        AIMessage(id="file-reference-response", content="Here is the report.", tool_calls=[{
            "name": "attach_ref", "id": "attach-report", "args": {
                "kind": "file", "name": name, "ref_id": str(path),
            },
        }]),
        ToolMessage(content="Attached", tool_call_id="attach-report"),
    ])
    endpoint = "**/api/files/ensure-registered"
    requests = []
    page.on("request", lambda req: requests.append(req.post_data_json)
            if req.url.endswith("/api/files/ensure-registered") else None)
    open_session(page, port, sid)
    chip = page.get_by_role("button", name=name, exact=True)
    with page.expect_response(lambda res: res.url.endswith("/api/files/ensure-registered")) as rejected:
        chip.press("Enter")
    assert rejected.value.json()["ok"] is False
    reason = page.get_by_role("alert").filter(has_text=missing)
    expect(reason).to_contain_text(name)
    expect(reason).to_contain_text(str(path))
    expect(chip).to_be_focused()

    page.route(endpoint, lambda route: route.abort("failed"), times=1)
    chip.click()
    expect(page.get_by_role("alert").filter(has_text=disconnected)).to_contain_text(name)
    assert len(requests) == 2

    path.write_text("# Recovered preview content", encoding="utf-8")
    chip.click()
    expect(page.get_by_role("heading", name="Recovered preview content", exact=True)).to_be_visible()
    assert requests == [{"session_id": sid, "path": str(path), "name": name}] * 3


def test_artifact_preview_explains_missing_file_record(app, tmp_path):
    from ginno_runtime.artifacts import add_artifact

    page, port, sid = app
    path = tmp_path / "unregistered-report.md"
    name = "Artifact without a file record"
    add_artifact("default", "file", name, str(path), sid)
    open_session(page, port, sid)
    row = page.get_by_text(name, exact=True)
    row.click()
    reason = page.get_by_role("alert").filter(has_text="No file record was found for this artifact")
    expect(reason).to_contain_text(name)
    expect(reason).to_contain_text(str(path))

    pattern = "**/api/files?project_slug=default"
    page.route(pattern, lambda route: route.abort("failed"), times=1)
    row.click()
    expect(page.get_by_role("alert").filter(has_text="Could not connect to the Ginno runtime")).to_contain_text(name)

    page.route(pattern, lambda route: route.fulfill(status=500, json={"detail": "fixture error"}), times=1)
    row.click()
    expect(page.get_by_role("alert").filter(has_text="Could not load the file record")).to_contain_text(name)


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


def test_compaction_blocks_sending_but_keeps_draft(app):
    page, port, sid = app
    sent = []
    page.on("websocket", lambda socket: socket.on("framesent", lambda frame: sent.append(json.loads(frame))))
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)

    composer = page.locator("textarea").first
    send = page.get_by_role("button", name="Send", exact=True)
    send_frame(page, sid, "context.compacting", stage="summarizing")
    composer.fill("Keep this next query as a draft")
    for stage in ("summarizing", "writing"):
        send_frame(page, sid, "context.compacting", stage=stage)
        expect(send).to_be_disabled()
        composer.press("Enter")
        expect(composer).to_have_value("Keep this next query as a draft")
        assert not any(frame.get("type") in ("invoke", "steer") for frame in sent)
    send_frame(page, sid, "context.compacting", stage="done")
    expect(send).to_be_enabled()
    expect(composer).to_have_value("Keep this next query as a draft")
    composer.press("Enter")
    expect(page.get_by_text("Keep this next query as a draft", exact=True)).to_be_visible()


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
    with page.expect_response(lambda response: f"/api/sessions/{sid}/history?turns=" in response.url) as restored:
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
    with page.expect_response(lambda response: f"/api/sessions/{sid}/history?turns=" in response.url) as restored:
        send_frame(page, sid, "turn.state", running=False)
    assert restored.value.ok
    expect(page.get_by_text("Reconciled report host", exact=True)).to_be_visible()
    expect(page.get_by_text("2/2", exact=True)).to_be_visible()
    expect_active_find(page, "Shared plan needle", row="checkpoint-host", within=second_plan)
    expect(field).to_be_focused()


def test_bounded_window_keeps_find_navigation_and_live_reading(app):
    page, port, sid = app
    messages = []
    for i in range(640):
        match = " Shared window needle" if i in (0, 320, 639) else ""
        messages.append(HumanMessage(id=f"window-user-{i}", content=f"Window request {i:04d}{match}"))
        if i == 320:
            messages.extend([
                AIMessage(id="window-tool", content="", tool_calls=[
                    {"name": "bash", "args": {"command": "printf fixture"}, "id": "window-giant"},
                ]),
                ToolMessage(id="window-tool-result", name="bash", tool_call_id="window-giant",
                            content="Giant tool output sentinel\n" + "fixture output line\n" * 1500),
            ])
        messages.append(AIMessage(id=f"window-answer-{i}", content=f"Window answer {i:04d}"))
    save_messages(sid, messages)
    other = harness._api(port, "POST", "/api/sessions", {
        "project_slug": "default", "workspace": "/tmp", "title": "Second window fixture",
    })["id"]
    save_messages(other, [HumanMessage(id="other-fixture", content="Second window fixture message")])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    scroll = page.locator("[data-chat-scroll]")
    rows = scroll.locator("[data-find-row]")
    latest = page.get_by_text("Window answer 0639", exact=True)
    expect(latest).to_be_in_viewport()
    expect(rows).to_have_count(80)
    scroll.evaluate('''el => {
        window.maxTranscriptRows = 0;
        new MutationObserver(() => {
            window.maxTranscriptRows = Math.max(window.maxTranscriptRows, el.querySelectorAll('[data-find-row]').length);
        }).observe(el, {childList: true, subtree: true});
    }''')

    # The tail window can emit a final scroll event after the find reveal has
    # requested an older window and before React commits it. Deliver that
    # ordering explicitly while the tail is still mounted.
    scroll.evaluate('''el => {
        window.staleTailScrolls = 0;
        const onReveal = event => {
            if (!event.detail.rowKey.includes('"window-user-0"')) return;
            window.removeEventListener('ginno:find-reveal-row', onReveal);
            el.scrollTop = el.scrollHeight;
            el.dispatchEvent(new Event('scroll'));
            window.staleTailScrolls++;
        };
        window.addEventListener('ginno:find-reveal-row', onReveal);
    }''')
    field = find_in_session(page, "Shared window needle")
    expect(page.get_by_text("1/3", exact=True)).to_be_visible()
    expect_active_find(page, "Shared window needle", row="window-user-0")
    assert page.evaluate("window.staleTailScrolls") == 1
    assert rows.count() <= 80
    expect(latest).to_have_count(0)
    field.press("Enter")
    expect(page.get_by_text("2/3", exact=True)).to_be_visible()
    expect_active_find(page, "Shared window needle", row="window-user-320")
    giant = scroll.locator('[data-msg-id="window-tool"]')
    fold = giant.get_by_role("button", name="tool · Running command · printf fixture", exact=False)
    expect(fold).to_have_attribute("aria-expanded", "false")
    fold.click()
    expect(fold).to_have_attribute("aria-expanded", "true")
    expect(giant.get_by_text("Giant tool output sentinel", exact=False)).to_be_visible()
    fold.click()
    expect(fold).to_have_attribute("aria-expanded", "false")
    field.press("Enter")
    expect(page.get_by_text("3/3", exact=True)).to_be_visible()
    expect_active_find(page, "Shared window needle", row="window-user-639")
    expect(field).to_be_focused()
    field.press("Escape")

    # Use the app's same-session message-id event, then move over two
    # overlapping ranges and return by keyboard without remounting all history.
    page.evaluate('window.dispatchEvent(new CustomEvent("ginno:scroll-to-message", {detail: "window-user-0"}))')
    first = scroll.locator('[data-msg-id="window-user-0"]')
    expect(first).to_be_in_viewport()
    expect(rows).to_have_count(80)
    for expected_first in (30, 60):
        page.get_by_test_id("load-later").press("Enter")
        expect(rows.first).to_have_attribute("data-msg-id", f"window-user-{expected_first}")
        expect(rows).to_have_count(80)
    page.get_by_test_id("load-earlier").press("Enter")
    expect(rows.first).to_have_attribute("data-msg-id", "window-user-30")
    reader = scroll.locator('[data-msg-id="window-user-60"]')
    expect(rows).to_have_count(80)
    expect(reader).to_be_focused()
    expect(reader).to_be_in_viewport()
    before = reader.bounding_box()["y"]

    send_frame(page, sid, "turn.start", turn_id="window-live", agent_id="dev")
    send_frame(page, sid, "token.delta", content="Full live response with window terminal sentinel")
    send_frame(page, sid, "message.end", turn_id="window-live", text="Full live response with window terminal sentinel")
    expect(reader).to_be_in_viewport()
    assert abs(reader.bounding_box()["y"] - before) <= 3
    expect(first).to_have_count(0)
    expect(latest).to_have_count(0)
    page.get_by_role("button", name="Jump to latest message", exact=True).press("Enter")
    live = page.get_by_text("Full live response with window terminal sentinel", exact=True)
    expect(live).to_be_in_viewport()
    expect(rows).to_have_count(80)
    expect(reader).to_have_count(0)
    expect(scroll).to_be_focused()
    assert page.evaluate("window.maxTranscriptRows") == 80

    page.evaluate('window.dispatchEvent(new CustomEvent("ginno:scroll-to-message", {detail: "window-user-0"}))')
    expect(first).to_be_in_viewport()
    page.evaluate("sid => window.__ginnoOpenSession(sid)", sid)
    expect(live).to_be_in_viewport()
    expect(first).to_have_count(0)

    # Session search stores an ID when switching conversations. The target
    # must survive the switch's initial tail pin and asynchronous transcript.
    page.evaluate("sid => window.__ginnoOpenSession(sid)", other)
    expect(page.get_by_text("Second window fixture message", exact=True)).to_be_in_viewport()
    page.evaluate('''sid => {
        sessionStorage.setItem("ginno-scroll-to-message", "window-user-0");
        window.__ginnoOpenSession(sid);
    }''', sid)
    expect(first).to_be_in_viewport()
    expect(rows).to_have_count(80)
    expect(latest).to_have_count(0)


def test_paged_history_defers_tool_details_and_search_index(app):
    page, port, sid = app
    history_path = f"/api/sessions/{sid}/history"
    body = "[exit 0]\n" + "First tool search sentinel\n" + "large past output\n" * 350
    messages = []
    for i in range(640):
        messages.extend([
            HumanMessage(id=f"history-user-{i}", content=f"History question {i}"),
            AIMessage(id=f"history-assistant-{i}", content=f"History answer {i}", tool_calls=[
                {"name": "bash", "args": {"command": f"printf fixture-{i}"}, "id": f"call-{i}"},
            ]),
            ToolMessage(id=f"history-output-{i}", tool_call_id=f"call-{i}",
                        content=body if i == 0 else "[exit 0]\n" + "ordinary output fragment\n" * 250),
        ])
    save_messages(sid, messages)
    requests = []
    page.on("request", lambda request: requests.append(request.url.split(f"127.0.0.1:{port}")[-1])
            if history_path in request.url else None)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    expect(page.get_by_text("History answer 639", exact=True)).to_be_in_viewport()
    assert requests == [f"{history_path}?turns=40"]
    expect(page.locator('[data-msg-id="history-user-0"]')).to_have_count(0)
    expect(page.locator('[data-msg-id="history-user-580"]')).to_have_count(0)
    page.get_by_test_id("load-earlier").click()
    expect(page.locator('[data-msg-id="history-user-580"]')).to_be_attached()
    assert len(requests) == 2 and "before=" in requests[1]
    assert all("index=" not in request and "/tools/" not in request for request in requests)

    # Index the saved synthetic checkpoint, then use the actual global search.
    from ginno_runtime.search_index import build_index

    assert build_index(force=True)["indexed"] == 1
    page.keyboard.press("Control+k")
    search = page.get_by_role("dialog", name="Search sessions")
    search.get_by_role("textbox").fill("First tool search sentinel")
    search.get_by_role("button", name="First tool search sentinel", exact=False).click()
    expect(search).to_have_count(0)
    first = page.locator('[data-msg-id="history-assistant-0"]')
    output = first.get_by_text("First tool search sentinel", exact=False)
    expect(output).to_be_visible(timeout=10_000)
    assert sum("/tools/" in request for request in requests) == 1
    assert all("index=" not in request for request in requests)
    first.get_by_role("button", name="Copy tool output", exact=True).click()
    assert page.evaluate("navigator.clipboard.readText()") == body

    field = find_in_session(page, "History question 0")
    expect(page.get_by_text("1/1", exact=True)).to_be_visible()
    expect_active_find(page, "History question 0", row="history-user-0")
    assert requests.count(f"{history_path}?index=true") == 1
    field.fill("First tool search sentinel")
    expect_active_find(page, "First tool search sentinel", row="history-assistant-0")
    assert sum("/tools/" in request for request in requests) == 1
    assert page.locator("[data-chat-scroll] [data-find-row]").count() <= 80


def test_paged_replay_renders_mermaid_image_and_lazy_diff(app, tmp_path):
    from PIL import Image

    from ginno_runtime.files.images import encode_images_marker

    page, port, sid = app
    img = tmp_path / "history-plot.png"
    Image.new("RGB", (16, 16), "blue").save(img)
    save_messages(sid, [
        HumanMessage(id="diagram-question", content="Draw diagram and update example"),
        AIMessage(id="diagram-answer", content="", tool_calls=[
            {"name": "bash", "args": {"command": "draw synthetic example"}, "id": "plot-call"},
            {"name": "edit_file", "args": {"path": "example.txt", "old": "old line\n", "new": "new line\n"},
             "id": "edit-call"},
        ]),
        ToolMessage(content="[exit 0]\n" + encode_images_marker([str(img)]), tool_call_id="plot-call"),
        ToolMessage(content="Synthetic edit receipt", tool_call_id="edit-call"),
        AIMessage(content="```mermaid\ngraph TD\n  A[Start] --> B[Finish]\n```"),
    ])
    tool_requests = []
    page.on("request", lambda req: tool_requests.append(req.url) if "/history/tools/" in req.url else None)
    page.route("**/history/tools/*", lambda route: route.fulfill(status=503, body="retry"), times=1)
    open_session(page, port, sid)
    row = page.locator('[data-msg-id="diagram-answer"]')
    expect(row.locator('[role="img"] svg .node')).to_have_count(2, timeout=15_000)
    page.wait_for_function('''() => {
        const img = document.querySelector('[data-msg-id="diagram-answer"] img');
        return img?.naturalWidth === 16;
    }''')
    assert tool_requests == []
    row.locator('[data-process-fold] > button').click()
    edit = row.locator('button[title^="edit_file"]')
    expect(edit).to_contain_text("+1")
    expect(edit).to_contain_text("−1")
    edit.click()
    row.get_by_role("button", name="Retry loading tool output").click()
    expect(row.locator(".diff").get_by_text("new line", exact=True)).to_be_visible()
    assert len(tool_requests) == 2 and len(set(tool_requests)) == 1


def test_find_tracks_live_continuation_in_replayed_bubble(app):
    page, port, sid = app
    save_messages(sid, [
        HumanMessage(id="resume-turn", content="Resume the reply"),
        AIMessage(id="resume-answer", content="Persisted half of the reply."),
    ])
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    row = page.locator('[data-msg-id="resume-answer"]')
    expect(row).to_contain_text("Persisted half of the reply.")
    with page.expect_response(lambda res: res.url.endswith(f"/{sid}/history?index=true")) as index:
        field = find_in_session(page, "Persisted half")
    assert index.value.ok
    expect_active_find(page, "Persisted half", row="resume-answer")

    send_frame(page, sid, "token.delta", turn_id="resume-turn", content=" Resumed text sentinel.")
    expect(row).to_contain_text("Resumed text sentinel.")
    field.fill("Resumed text sentinel")
    expect(page.get_by_text("1/1", exact=True)).to_be_visible()
    expect_active_find(page, "Resumed text sentinel", row="resume-answer")


def test_find_matches_visible_markdown_and_skips_hidden_link(app):
    page, port, sid = app
    save_messages(sid, [
        HumanMessage(id="md-turn", content="Present the result"),
        AIMessage(id="md-answer", content=(
            "Displayed **emphasis** phrase.\n\n"
            "[Visible label](https://example.com/hidden-link-sentinel)"
        )),
    ])
    open_session(page, port, sid)
    row = page.locator('[data-msg-id="md-answer"]')
    expect(row).to_contain_text("Displayed emphasis phrase.")
    with page.expect_response(lambda res: res.url.endswith(f"/{sid}/history?index=true")) as index:
        field = find_in_session(page, "Displayed emphasis phrase")
    assert index.value.ok
    expect(page.get_by_text("1/1", exact=True)).to_be_visible()
    expect_active_find(page, "Displayed emphasis phrase", row="md-answer")
    field.fill("Visible label")
    expect_active_find(page, "Visible label", row="md-answer")
    field.fill("hidden-link-sentinel")
    expect(page.get_by_text("No hits", exact=True)).to_be_visible()
