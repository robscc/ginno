"""A real edit_file call renders its diff inside the packaged chat."""

import json
import re

import pytest
import test_packaged_ui_tasks_overview as harness
from test_packaged_ui_transcript_event_rows import capture_socket, open_session, wait_for_session_socket

from ginno_runtime.paths import session_files_dir

pytestmark = pytest.mark.e2e


def test_edit_diff_renders(tmp_path, monkeypatch):
    harness._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    filename = "demo.py"
    scripts = tmp_path / "scripts.json"
    scripts.write_text(json.dumps([
        {"content": "", "tool_calls": [
            {"name": "write_file", "args": {"path": filename, "content": "x=1\ny=2\n"}},
        ]},
        {"content": "", "tool_calls": [
            {"name": "edit_file", "args": {"path": filename, "old": "y=2", "new": "y=42"}},
        ]},
        {"content": "Updated demo.py."},
    ]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port)
    try:
        harness._wait_health(port)
        sid = harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Edit diff",
        })["id"]
        target = session_files_dir("default", sid) / filename
        with sync_playwright() as pw:
            browser, page = harness._new_page(pw)
            try:
                capture_socket(page)
                open_session(page, port, sid)
                wait_for_session_socket(page, sid, require_turn_state=True)
                composer = page.locator("textarea")
                composer.fill("Fix demo.py")
                composer.press("Enter")
                expect(page.get_by_text("Updated demo.py.", exact=True)).to_be_visible()
                assert target.read_text(encoding="utf-8") == "x=1\ny=42\n"

                process = page.locator("[data-process-fold]")
                toggle = process.locator(":scope > button")
                edit = process.get_by_title(re.compile(r"^edit_file — "))
                expect(toggle).to_contain_text("Completed 2 actions")
                expect(toggle).to_have_attribute("aria-expanded", "false")
                expect(edit).not_to_be_visible()
                toggle.click()
                expect(toggle).to_have_attribute("aria-expanded", "true")
                expect(edit).to_have_attribute("aria-expanded", "true")
                expect(edit.get_by_text("+1", exact=True)).to_be_visible()
                expect(edit.get_by_text("−1", exact=True)).to_be_visible()

                diff = edit.locator("..").locator(".diff")
                expect(diff.get_by_text("@@ -1 +1 @@", exact=True)).to_be_visible()
                for line, marker in (("y=2", "−"), ("y=42", "+")):
                    content = diff.get_by_text(line, exact=True)
                    expect(content).to_be_visible()
                    expect(content.locator("..").get_by_text(marker, exact=True)).to_be_visible()
            finally:
                browser.close()
    finally:
        harness._stop(proc, log)
