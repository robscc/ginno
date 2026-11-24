"""Assistant Markdown file citations open the mounted file at its line."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


def test_markdown_file_links_in_chat_and_history(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    workspace = tmp_path / "workspace"
    nested = workspace / "sample files"
    nested.mkdir(parents=True)
    source = (nested / "示例.py").resolve()
    source.write_text("\n".join(f"# Sample line {i}" for i in range(1, 25)), encoding="utf-8")
    outside = (tmp_path / "outside.py").resolve()
    web_url = "https://example.test/docs"
    preview_url = "http://127.0.0.1:28632/excel"
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([{"content": (
        f"[Local line](<{source}:12>) · [File only]({quote(str(source))}) · "
        f"[Outside file]({outside}:6) · [Web docs]({web_url}) · [Local preview]({preview_url})"
    )}]))
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        mounts = [smoke._api(port, "POST", "/api/folders", {"path": str(path)})["folder"]["id"]
                  for path in (workspace, nested)]
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(workspace), "title": "Markdown file links",
            "context_folders": mounts, "primary_folder": mounts[0],
        })
        session_id = session["id"]
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session_id)})")
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                expect(page.get_by_text("Markdown file links", exact=True).first).to_be_visible()
                composer = page.locator("textarea").first
                composer.fill("Show a local citation and a web link.")
                composer.press("Enter")
                local = page.get_by_role("button", name="Local line", exact=True)
                expect(local).to_have_attribute("title", f"Open {source}:12 in the code panel", timeout=15_000)

                with page.expect_request(lambda request: urlsplit(request.url).path == "/api/code/read") as read:
                    local.press("Enter")
                query = parse_qs(urlsplit(read.value.url).query)
                assert query["root"] == [mounts[1]]
                assert query["path"] == [source.name]
                assert query["session_id"] == [session_id]
                expect(page.locator(".monaco-editor .active-line-number")).to_have_text("12")
                expect(page.get_by_role("button", name="File only", exact=True)).to_have_attribute(
                    "title", f"Open {source} in the code panel",
                )
                page.get_by_role("button", name="Outside file", exact=True).click()
                expect(page.get_by_text("This file is outside the current workspace root")).to_be_visible()

                page.route("**/api/open-external", lambda route: route.fulfill(json={"ok": True}))
                web = page.get_by_role("link", name="Web docs", exact=True)
                expect(web).to_have_attribute("href", web_url)
                with page.expect_request("**/api/open-external") as navigation:
                    web.click()
                assert navigation.value.post_data_json == {"url": web_url}
                with page.expect_request("**/api/open-external") as preview:
                    page.get_by_role("link", name="Local preview", exact=True).click()
                assert preview.value.post_data_json == {"url": preview_url, "local_preview": True}
                assert len(page.context.pages) == 1

                page.reload(wait_until="load")
                expect(local).to_be_visible(timeout=15_000)
                with page.expect_request(lambda request: urlsplit(request.url).path == "/api/code/read"):
                    local.click()
                expect(page.locator(".monaco-editor .active-line-number")).to_have_text("12")
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
