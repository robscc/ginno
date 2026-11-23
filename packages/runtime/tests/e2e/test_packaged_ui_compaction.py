"""The chat shows one status for a real background /compact."""

from __future__ import annotations

import json
import re

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


def test_compaction_shows_activity_without_estimated_progress(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    settings = smoke._settings()
    settings["context"] = {"compact_keep_turns": 1, "compaction_enabled": False}
    monkeypatch.setattr(smoke, "_settings", lambda: settings)
    monkeypatch.setenv("GINNO_FAKE_LLM_DELAY", "2")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", json.dumps([
        {"content": "The first answer."},
        {"content": "The second answer."},
        {"content": "A summary of the early discussion."},
    ]))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                composer = page.locator("textarea").first
                for request, reply in [("First question", "The first answer."), ("Second question", "The second answer.")]:
                    composer.fill(request)
                    composer.press("Enter")
                    expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=15_000)
                    expect(page.get_by_title(re.compile(r"^Stop"))).to_have_count(0)

                composer.fill("/compact")
                composer.press("Escape")  # dismiss the slash-command suggestions
                composer.press("Enter")
                active = page.get_by_role("status").filter(has_text="Summarizing conversation history")
                expect(active).to_be_visible(timeout=15_000)
                expect(page.get_by_text("Summarizing conversation history…", exact=True)).to_have_count(1)
                expect(active).not_to_contain_text(re.compile(r"\d+%|\bs left"))
                expect(page.get_by_text(re.compile(r"Context compacted"))).to_be_visible(timeout=15_000)
                expect(active).to_have_count(0)
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
