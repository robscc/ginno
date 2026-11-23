"""Responses summaries stay at their model request during chat and after reload."""

from __future__ import annotations

import json
import re

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

SUMMARY = "Compare the two choices before answering."
ANSWER = "Choose the second option."


def test_reasoning_summary_in_chat_and_history(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    def reasoning(*parts):
        return {"type": "reasoning", "summary": [
            {"type": "summary_text", "text": part} for part in parts
        ]}

    before = f"    value = '{'a' * 240}'"
    after = f"    value = '{'b' * 240}'"
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([
        {
            "content": [reasoning("**Prepare sample**")],
            "tool_calls": [{"name": "write_file", "args": {"path": "demo.py", "content": before + "\n"}}],
        },
        {
            "content": [reasoning("**Update value**")],
            "tool_calls": [{"name": "edit_file", "args": {"path": "demo.py", "old": before, "new": after}}],
        },
        {
            "content": [reasoning("**Review options**", SUMMARY), {"type": "text", "text": ANSWER}],
        },
    ]))
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                ta = page.locator("textarea").first
                ta.fill("Compare two options")
                ta.press("Enter")

                def check_transcript():
                    answer = page.get_by_text(ANSWER, exact=True)
                    expect(answer).to_be_visible(timeout=15_000)
                    panels = page.get_by_role("button", name=re.compile(r"^Reasoning summary"))
                    expect(panels).to_have_count(3)
                    write = page.get_by_title(re.compile(r"^write_file — "))
                    edit = page.get_by_title(re.compile(r"^edit_file — "))
                    ordered = [panels.nth(0), write, panels.nth(1), edit, panels.nth(2), answer]
                    for left, right in zip(ordered, ordered[1:]):
                        assert left.evaluate(
                            "(el, next) => !!(el.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING)",
                            right.element_handle(),
                        )
                    for index in range(3):
                        expect(panels.nth(index)).to_have_attribute("aria-expanded", "false")
                    expect(panels.nth(0)).to_contain_text("Prepare sample")
                    expect(panels.nth(1)).to_contain_text("Update value")
                    expect(panels.nth(2)).to_contain_text("2 parts")
                    panels.nth(2).click()
                    expect(page.get_by_text("Review options", exact=True)).to_be_visible()
                    expect(page.get_by_role("listitem").filter(has_text=SUMMARY)).to_be_visible()
                    expect(panels.nth(1)).to_have_attribute("aria-expanded", "false")

                    # edit_file arguments are fragments, so neither value has a newline.
                    card = edit.locator("..")
                    diff = card.locator(".diff")
                    removed = diff.get_by_text(before.strip(), exact=True)
                    added = diff.get_by_text(after.strip(), exact=True)
                    expect(removed.locator("..").get_by_text("−", exact=True)).to_be_visible()
                    expect(added.locator("..").get_by_text("+", exact=True)).to_be_visible()
                    for view in ("unified", "split"):
                        card.get_by_role("button", name=view, exact=True).click()
                        for line, text in [(removed, before), (added, after)]:
                            assert line.text_content() == text
                            assert line.evaluate("""el => {
                                const range = document.createRange();
                                range.selectNodeContents(el);
                                return range.getClientRects().length > 1;
                            }""")
                        assert diff.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                    assert removed.bounding_box()["y"] == added.bounding_box()["y"]

                check_transcript()
                page.reload(wait_until="load")
                check_transcript()
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
