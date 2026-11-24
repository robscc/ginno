"""Render a scheduled run's text widget on first open and history refresh."""

from __future__ import annotations

import json
import time

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


def test_scheduled_result_recovers_inline_widget(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    title = "Today’s repository activity"
    data = {"title": title, "items": [{"label": "Team's repo", "value": "2 commits", "status": "done"}]}
    marker = f"<ginno-block type=\"render_widget\" data='{json.dumps(data)}' summary=\"Two commits\"></ginno-block>"
    scripts = tmp_path / "widget-response.json"
    scripts.write_text(json.dumps([{"content": f"Latest results\n\n{marker}\n\nReview complete."}]))
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        task = smoke._api(port, "POST", "/api/schedule/tasks", {
            "name": "Scheduled widget fallback",
            "target": {"type": "prompt", "prompt": "Report recent commits"},
            "schedule": {"kind": "interval", "minutes": 60},
        })
        task_id = task["id"]
        run = smoke._api(port, "POST", f"/api/schedule/tasks/{task_id}/run")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            rows = smoke._api(port, "GET", f"/api/schedule/runs?task_id={task_id}")["rows"]
            row = next((row for row in rows if row["run_id"] == run["run_id"]), None)
            if row and row["status"] != "running":
                break
            time.sleep(0.15)
        else:
            pytest.fail("scheduled run did not finish")
        assert row["status"] == "ok"
        session_id = row["session_id"]
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f'localStorage.setItem("ginno-last-session", {json.dumps(session_id)});')
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                for attempt in range(2):
                    if attempt:
                        page.reload(wait_until="load")
                    expect(page.get_by_text(title, exact=True)).to_be_visible()
                    expect(page.get_by_text("Team's repo", exact=True)).to_be_visible()
                    expect(page.get_by_text("Review complete.", exact=True)).to_be_visible()
                    expect(page.get_by_text("Two commits", exact=True)).to_be_visible()
                    expect(page.get_by_text("<ginno-block", exact=False)).to_have_count(0)
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
