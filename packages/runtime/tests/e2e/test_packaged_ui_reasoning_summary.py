"""Responses reasoning summaries are visible during chat and after reload."""

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

    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([{"content": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "**Review options**"}, {"type": "summary_text", "text": SUMMARY}]},
        {"type": "text", "text": ANSWER},
    ]}]))
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                title = "Compare two options"
                ta = page.locator("textarea").first
                ta.fill(title)
                ta.press("Enter")
                expect(page.get_by_text(ANSWER, exact=True)).to_be_visible(timeout=15_000)

                def check_summary():
                    panel = page.get_by_role("button", name=re.compile(r"^Reasoning summary"))
                    expect(panel).to_have_count(1)
                    expect(panel).to_have_attribute("aria-expanded", "false")
                    expect(panel).to_contain_text("2 parts")
                    panel.click()
                    expect(page.get_by_text("Review options", exact=True)).to_be_visible()
                    expect(page.get_by_role("listitem").filter(has_text=SUMMARY)).to_be_visible()

                check_summary()
                page.reload(wait_until="load")
                expect(page.get_by_text(ANSWER, exact=True)).to_be_visible(timeout=15_000)
                check_summary()
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
