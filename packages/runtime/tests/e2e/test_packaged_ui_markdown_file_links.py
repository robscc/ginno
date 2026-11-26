"""Assistant Markdown file citations open the mounted file at its line.

A citation outside every workspace root opens the code panel on the design §3.2
guidance; its "Add to workspace" action mounts the file's folder and then opens
the file through the regular rooted path.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import test_packaged_ui_tasks_overview as smoke

from ginno_runtime.checkpointer import FileCheckpointer

pytestmark = pytest.mark.e2e


def _wait_for_persisted_reply(session_id, markdown):
    # Read the checkpoint from this pytest process, independently of sidecar caches.
    checkpointer = FileCheckpointer("default")
    config = {"configurable": {"thread_id": session_id}}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        saved = checkpointer.get_tuple(config)
        if saved and any(
            msg.type == "ai" and msg.content == markdown
            for msg in saved.checkpoint["channel_values"].get("messages", [])
        ):
            return
        time.sleep(0.05)
    pytest.fail(f"Assistant Markdown was not written to the checkpoint for {session_id}")


def test_markdown_file_links_in_chat_and_history(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    workspace = tmp_path / "workspace"
    nested = workspace / "sample files"
    nested.mkdir(parents=True)
    source = (nested / "示例.py").resolve()
    source_text = "\n".join(f"# Sample line {i}" for i in range(1, 25))
    source.write_text(source_text, encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = (outside_dir / "outside.py").resolve()
    outside_dir = outside.parent
    outside.write_text("# Outside the mounted workspace\n", encoding="utf-8")
    web_url = "https://example.test/docs"
    preview_url = "http://127.0.0.1:28632/excel"
    markdown = (
        f"[Local line](<{source}:12>) · [File only]({quote(str(source))}) · "
        f"[Outside file]({outside}:6) · [Web docs]({web_url}) · [Local preview]({preview_url})"
    )
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([{"content": markdown}]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        mounts = [smoke._api(port, "POST", "/api/folders", {"path": str(path)})["folder"]["id"]
                  for path in (workspace, nested)]
        title = "Markdown file links"
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(workspace), "title": title,
            "context_folders": mounts, "primary_folder": mounts[0],
        })
        session_id = session["id"]
        history_url = f"http://127.0.0.1:{port}/api/sessions/{session_id}/history?turns=40"
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                code_reads = []

                def record_code_read(request):
                    if urlsplit(request.url).path == "/api/code/read":
                        code_reads.append(parse_qs(urlsplit(request.url).query))

                page.on("request", record_code_read)
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session_id)})")
                # The sidebar title can appear while the composer is still on Home.
                with (
                    page.expect_response(history_url) as initial_history,
                    page.expect_websocket(
                        lambda ws: urlsplit(ws.url).path == f"/api/ws/sessions/{session_id}",
                    ) as session_socket,
                ):
                    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                assert initial_history.value.json()["messages"] == []

                prompt = "Show a local citation and a web link."
                composer = page.locator("textarea:visible").first
                composer.fill(prompt)
                with session_socket.value.expect_event(
                    "framesent", lambda frame: json.loads(frame).get("type") == "invoke",
                ) as invocation:
                    composer.press("Enter")
                assert json.loads(invocation.value)["message"] == prompt

                local = page.get_by_role("button", name="Local line", exact=True)
                expect(local).to_have_attribute("title", f"Open {source}:12 in the code panel", timeout=15_000)
                active_line = page.locator(".monaco-editor .active-line-number")
                expected_query = {
                    "project_slug": ["default"], "session_id": [session_id],
                    "root": [mounts[1]], "path": [source.name],
                }

                def assert_code_read(click, count):
                    with page.expect_response(lambda res: urlsplit(res.url).path == "/api/code/read") as read:
                        click()
                    response = read.value
                    assert response.ok
                    body = response.json()
                    assert body["ok"] and body["text"] == source_text
                    assert code_reads[-count:] == [expected_query] * count

                file_only = page.get_by_role("button", name="File only", exact=True)
                expect(file_only).to_have_attribute("title", f"Open {source} in the code panel")
                assert_code_read(file_only.click, 1)
                expect(active_line).to_have_text("1")
                local.press("Enter")
                expect(active_line).to_have_text("12")
                assert code_reads == [expected_query]

                page.get_by_role("button", name="Outside file", exact=True).click()
                guidance = page.get_by_test_id("code-outside-root")
                outside_text = guidance.get_by_text("This file is outside the current workspace root")
                expect(outside_text).to_be_visible()
                expect(outside_text).to_have_attribute("title", str(outside))
                assert code_reads == [expected_query], "an out-of-root click must not read any file"

                add_folder = guidance.get_by_role("button", name="Add to workspace", exact=True)
                expect(add_folder).to_have_attribute(
                    "title", f"Add {outside_dir} to this session's workspace"
                )
                context_path = f"/api/sessions/{session_id}/context"
                with (
                    page.expect_request(
                        lambda r: r.method == "POST" and urlsplit(r.url).path == "/api/folders"
                    ) as folder_request,
                    page.expect_request(
                        lambda r: r.method == "PUT" and urlsplit(r.url).path == context_path
                    ) as context_request,
                    page.expect_response(
                        lambda res: urlsplit(res.url).path == "/api/code/read"
                    ) as opened,
                ):
                    add_folder.click()
                assert folder_request.value.post_data_json == {
                    "path": str(outside_dir), "access": "rw", "load_rules": True,
                }
                folder = next(
                    f for f in smoke._api(port, "GET", "/api/folders")["folders"]
                    if f["path"] == str(outside_dir)
                )
                assert context_request.value.post_data_json == {
                    "folder_ids": [*mounts, folder["id"]], "primary_id": mounts[0],
                }
                opened_query = {
                    "project_slug": ["default"], "session_id": [session_id],
                    "root": [folder["id"]], "path": ["outside.py"],
                }
                assert opened.value.ok
                assert opened.value.json()["text"] == "# Outside the mounted workspace\n"
                assert code_reads == [expected_query, opened_query]
                expect(guidance).to_have_count(0)
                meta = smoke._api(port, "GET", f"/api/sessions/{session_id}")
                assert meta["primary_folder"] == mounts[0]
                assert {d["path"]: d["id"] for d in meta["context_dirs"]}[str(outside_dir)] == folder["id"]

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

                _wait_for_persisted_reply(session_id, markdown)
                with page.expect_response(history_url) as replay:
                    page.reload(wait_until="load")
                assert replay.value.ok
                assert any(
                    block == {"kind": "text", "text": markdown}
                    for message in replay.value.json()["messages"] if message["role"] == "assistant"
                    for block in message["blocks"]
                )
                expect(local).to_be_visible(timeout=15_000)
                assert_code_read(local.click, 1)
                assert code_reads == [expected_query, opened_query, expected_query]
                expect(active_line).to_have_text("12")
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
