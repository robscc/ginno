"""Link reference chips route mouse and keyboard activation to the desktop opener."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


def test_link_refs_open_local_preview_and_public_url(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    local_url = "http://127.0.0.1:28632/workbench"
    public_url = "https://example.test/docs"
    reply = (
        "Here are two links.\n"
        f'<attach_ref kind="link" name="Workbench" ref_id="{local_url}"/>\n'
        f'<attach_ref kind="link" name="Docs" ref_id="{public_url}"/>'
    )
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([{"content": reply}]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        title = "Link reference chips"
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": title,
        })
        session_id = session["id"]
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session_id)})")
                # Capture the real UI request without launching an OS browser.
                page.route("**/api/open-external", lambda route: route.fulfill(json={"ok": True}))
                with page.expect_websocket(
                    lambda ws: urlsplit(ws.url).path == f"/api/ws/sessions/{session_id}",
                ):
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                composer = page.locator("textarea:visible").first
                composer.fill("Show link references.")
                composer.press("Enter")

                local = page.get_by_role("button", name="Workbench", exact=True)
                expect(local).to_be_visible(timeout=15_000)
                with page.expect_request("**/api/open-external") as mouse_open:
                    local.click()
                assert mouse_open.value.post_data_json == {"url": local_url, "local_preview": True}

                local.focus()
                expect(local).to_be_focused()
                for key in ("Enter", "Space"):
                    with page.expect_request("**/api/open-external") as keyboard_open:
                        page.keyboard.press(key)
                    assert keyboard_open.value.post_data_json == {"url": local_url, "local_preview": True}

                with page.expect_request("**/api/open-external") as public_open:
                    page.get_by_role("button", name="Docs", exact=True).click()
                assert public_open.value.post_data_json == {"url": public_url}
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
