"""Subagent reports and plan notes fold into the assistant step in front of them.

Design A (2026-11-25): a run of reports after an assistant message is attached
to that bubble and folds into its 「执行过程」 group, together with the bubble's
own tool steps. Reports that need the user (failed / stopped / waiting) are
never folded: they render as cards below the fold and are counted in its header.
The result body renders as Markdown behind an expand toggle instead of a
scrolling pre-wrap wall, and a plan note shows item status changes only.

Both delivery paths run against the real sidecar:

* replay — result envelopes persisted in the checkpoint (history after reload);
* live — ``subagent.status`` frames on the session socket.
"""

from __future__ import annotations

import json

import pytest
import test_packaged_ui_tasks_overview as harness
import test_packaged_ui_transcript_event_rows as rows
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.e2e

REPORT = "## 证据\n\n- `pytest` 联跑 **58 项通过**\n"
REPLY = "已派单，等结果。"


@pytest.fixture
def app(tmp_path, monkeypatch):
    harness._require_runtime()
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([{"content": REPLY}]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port)
    try:
        harness._wait_health(port)
        session = harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Report fold",
        })
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            # Pin the copy (settings default to en; assertions use the same).
            page.add_init_script("window.localStorage.setItem('ginno.language', 'en')")
            try:
                yield page, port, session["id"]
            finally:
                browser.close()
    finally:
        harness._stop(proc, log)


def assert_markdown_body_without_inner_scroll(card):
    """The report body is rendered Markdown, and it is not its own scroll box."""
    expect(card.get_by_role("heading", name="证据")).to_be_visible()
    expect(card.locator("strong", has_text="58 项通过")).to_be_visible()
    expect(card.locator("code", has_text="pytest")).to_be_visible()
    expect(card.get_by_text("## 证据", exact=False)).to_have_count(0)
    assert card.evaluate(
        """el => {
            const body = el.querySelector('[data-report-body]');
            return !!body && getComputedStyle(body).overflowY === 'visible'
                && body.scrollHeight <= body.clientHeight + 1;
        }"""
    )


def test_persisted_reports_fold_after_reload(app):
    page, port, sid = app
    rows.save_messages(sid, [
        HumanMessage(id="u1", content="把前端尾项收掉"),
        AIMessage(id="a1", content="先看一眼现状。",
                  tool_calls=[{"id": "call_read", "name": "read_file", "args": {"path": "app.py"}}]),
        ToolMessage(id="t1", content="ok", tool_call_id="call_read"),
        AIMessage(id="a2", content=REPLY),
        HumanMessage(id="r1", content=(
            f'<ginno_subagent_result session="child-ok" goal="整理证据">{REPORT}</ginno_subagent_result>'
            '<ginno_plan_update plan="pl_doc">[plan update] Task list status changed:\n'
            '- [done] it_9 文档已更新\n</ginno_plan_update>'
            '<ginno_plan_update plan="pl_fold">[plan update] Task list status changed:\n'
            '- [failed] it_2 联调 ESM 解析失败\n</ginno_plan_update>'
        ), additional_kwargs={"ginno_subagent_result": "child-ok"}),
        AIMessage(id="a3", content="it_2 失败，我来重试。"),
    ])
    rows.open_session(page, port, sid)

    # Replay puts the envelopes at the head of the NEXT bubble; they fold at the
    # tail of the step in front of it — the same spot the live path uses.
    step = page.locator('[data-msg-id="a1"]')
    expect(step.get_by_text(REPLY, exact=True)).to_be_visible(timeout=15_000)
    reply = page.locator('[data-msg-id="a3"]')
    expect(reply.get_by_text("it_2 失败，我来重试。")).to_be_visible()
    expect(reply.locator("[data-process-fold]")).to_have_count(0)
    # One fold in the whole transcript, and it lives inside that first step.
    expect(page.locator("[data-process-fold]")).to_have_count(1)

    fold = step.locator("[data-process-fold] > button", has_text="subagent reports")
    expect(fold).to_contain_text("1 subagent reports")
    expect(fold).to_contain_text("1 failed")

    # Red line: the failed plan note is a card below the fold, readable without
    # opening anything — and it no longer repeats the model-facing header.
    failed_note = step.get_by_test_id("plan-update-card")
    expect(failed_note).to_be_visible()
    expect(failed_note).to_contain_text("it_2")
    expect(failed_note).to_contain_text("Failed")
    expect(failed_note).to_contain_text("ESM 解析失败")
    expect(step.get_by_text("Task list status changed", exact=False)).to_have_count(0)
    expect(step.get_by_text("[plan update]", exact=False)).to_have_count(0)

    # The success report and the settled plan note are folded away.
    expect(step.get_by_text("58 项通过", exact=False)).to_have_count(0)
    expect(step.get_by_test_id("plan-update-row")).to_have_count(0)
    fold.click()

    plan_row = step.get_by_test_id("plan-update-row")
    expect(plan_row).to_contain_text("it_9")
    expect(plan_row).to_contain_text("Completed")
    expect(plan_row).not_to_contain_text("Task list status changed")

    card = step.get_by_test_id("subagent-result-card")
    expect(card).to_be_visible()
    expect(card).not_to_have_attribute("data-result-open", "")
    card.locator(":scope > button").first.click()
    assert_markdown_body_without_inner_scroll(card)


def test_live_reports_fold_and_failures_stay_visible(app):
    page, port, sid = app
    rows.capture_socket(page)
    rows.open_session(page, port, sid)
    rows.wait_for_session_socket(page, sid)
    page.evaluate(
        """sid => {
            window.__reportFoldEvents = [];
            const ws = window.__transcriptTestSockets.find(ws =>
                new URL(ws.url).pathname.endsWith('/' + sid));
            ws.addEventListener('message', event => {
                try { window.__reportFoldEvents.push(JSON.parse(event.data).event); } catch {}
            });
        }""",
        sid,
    )
    composer = page.locator("textarea:visible").first
    composer.fill("开始")
    composer.press("Enter")
    page.wait_for_function("window.__reportFoldEvents.includes('message.end')", timeout=30_000)

    rows.send_frame(page, sid, "subagent.status", session_id="child-live-ok", parent_session_id=sid,
                    status="done", goal="整理证据", result_summary=REPORT)
    step = page.locator("[data-msg-id]").filter(has_text=REPLY)
    fold = step.locator("[data-process-fold] > button", has_text="subagent reports")
    expect(fold).to_contain_text("1 subagent reports")
    expect(page.get_by_text("58 项通过", exact=False)).to_have_count(0)

    # Red line, with the fold still closed: a failed report is on screen at once.
    rows.send_frame(page, sid, "subagent.status", session_id="child-live-bad", parent_session_id=sid,
                    status="failed", goal="联调", result_summary="", error="boom-sentinel")
    # A status frame carries no goal: the card is identified by its error text.
    failed = page.get_by_test_id("subagent-result-card").filter(has_text="boom-sentinel")
    expect(failed).to_be_visible()
    expect(failed).to_contain_text("Failed")
    expect(failed).to_contain_text("boom-sentinel")
    expect(fold).to_contain_text("1 failed")
    expect(fold).to_have_attribute("aria-expanded", "false")

    fold.click()
    # Only the successful report sits inside the group; the failed one is outside.
    card = step.locator("[data-process-fold]", has_text="subagent reports").get_by_test_id("subagent-result-card")
    card.locator(":scope > button").first.click()
    assert_markdown_body_without_inner_scroll(card)
    # Same placement as replay: one fold in the transcript, inside the step in front of it.
    expect(page.locator("[data-process-fold]")).to_have_count(1)
