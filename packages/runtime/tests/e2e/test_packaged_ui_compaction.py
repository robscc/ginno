"""Session activity in the sidebar and TopBar against an isolated sidecar."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


@pytest.fixture
def held_summary():
    release = threading.Event()

    class ModelGateway(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            summary = any(
                message["role"] == "system" and "You are the conversation summarizer" in message["content"]
                for message in request["messages"]
            )
            if summary:
                if not release.wait(45):
                    self.send_error(504)
                    return
                text = "A summary of the early discussion."
            else:
                prompt = next(message["content"] for message in reversed(request["messages"]) if message["role"] == "user")
                text = "The first answer." if prompt == "First question" else "The second answer."
            common = {"id": "activity-fixture", "created": 1, "model": request["model"]}
            if request.get("stream"):
                chunk = {**common, "object": "chat.completion.chunk"}
                events = [
                    dict(chunk, choices=[{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]),
                    dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
                ]
                data = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                data = json.dumps(dict(common, object="chat.completion", choices=[{
                    "index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop",
                }]))
                content_type = "application/json"
            body = data.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), ModelGateway) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", release
        finally:
            release.set()
            server.shutdown()
            thread.join()


def test_compaction_shows_activity_without_estimated_progress(tmp_path, monkeypatch, held_summary):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    model_url, release = held_summary
    settings = smoke._settings()
    settings["context"] = {"compact_keep_turns": 1, "compaction_enabled": False}
    settings["providers"]["custom"].update(base_url=model_url, default_model="m", timeout_s=50)
    monkeypatch.setattr(smoke, "_settings", lambda: settings)
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port, fake_llm=False)
    try:
        smoke._wait_health(port)
        first, other = "Activity under compaction", "Unrelated idle conversation"
        sid, _ = [smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": title,
            "provider": "custom", "model": "m",
        })["id"] for title in (first, other)]
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.emulate_media(reduced_motion="no-preference")
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                first_row = page.locator(".nav-item").filter(has=page.get_by_text(first, exact=True))
                other_row = page.locator(".nav-item").filter(has=page.get_by_text(other, exact=True))
                top = page.locator(".topbar-session-activity")
                first_row.get_by_text(first, exact=True).click()
                composer = page.locator("textarea").first
                for request, reply in [("First question", "The first answer."), ("Second question", "The second answer.")]:
                    composer.fill(request)
                    composer.press("Enter")
                    expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=15_000)
                    expect(page.get_by_title(re.compile(r"^Stop"))).to_have_count(0)

                composer.fill("/compact")
                composer.press("Escape")  # dismiss the slash-command suggestions
                composer.press("Enter")
                transcript_status = page.get_by_role("status").filter(has_text="Summarizing conversation history")
                expect(transcript_status).to_be_visible()
                expect(page.get_by_text("Summarizing conversation history…", exact=True)).to_have_count(1)
                expect(transcript_status).not_to_contain_text(re.compile(r"\d+%|\bs left"))
                assert smoke._api(port, "GET", "/api/running")["compacting"] == [sid]
                for container in (first_row, top):
                    badge = container.get_by_role("img", name="Compacting conversation")
                    expect(badge).to_have_css("width", "16px")
                    expect(badge.locator(".wheel")).to_have_css("animation-name", "ginno-compaction-wheel")
                expect(other_row.get_by_role("img", name="Compacting conversation")).to_have_count(0)

                # Emulate Reduced Motion while the same real compaction is held.
                page.emulate_media(reduced_motion="reduce")
                expect(top.get_by_role("img", name="Compacting conversation").locator(".wheel")).to_have_css("animation-name", "none")
                other_row.get_by_text(other, exact=True).click()
                expect(top.get_by_role("img", name="Idle")).to_be_visible()
                expect(first_row.get_by_role("img", name="Compacting conversation")).to_be_visible()
                release.set()
                expect(first_row.get_by_role("img", name="Compacting conversation")).to_have_count(0)
                assert smoke._api(port, "GET", "/api/running")["compacting"] == []
                first_row.get_by_text(first, exact=True).click()
                expect(page.get_by_text(re.compile(r"Context compacted"))).to_be_visible(timeout=15_000)
                expect(transcript_status).to_have_count(0)
                expect(top.get_by_role("img", name="Idle")).to_be_visible()
            finally:
                release.set()
                browser.close()
    finally:
        smoke._stop(proc, log)


def test_topbar_waves_while_ask_user_waits(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", json.dumps([
        {"content": "", "tool_calls": [{"name": "ask_user", "args": {
            "question": "Which format?", "options": ["Markdown", "Plain text"],
        }}]},
        {"content": "Format confirmed."},
    ]))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Format conversation",
        })
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.emulate_media(reduced_motion="no-preference")
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                page.get_by_text(session["title"], exact=True).first.click()
                composer = page.locator("textarea").first
                composer.fill("Ask me to choose a format.")
                composer.press("Enter")
                top = page.locator(".topbar-session-activity")
                wait = top.get_by_role("img", name="Waiting for your answer")
                expect(wait.locator("svg")).to_have_css("animation-name", "ginno-waiting-hand-wave")
                expect(top.locator(".running-indicator")).to_have_count(0)
                expect(page.locator(".nav-item").get_by_role("img", name="Waiting for your answer")).to_be_visible()
                assert [entry["id"] for entry in smoke._api(port, "GET", "/api/running")["waiting"]] == [session["id"]]
                page.emulate_media(reduced_motion="reduce")
                expect(wait.locator("svg")).to_have_css("animation-name", "none")
                page.get_by_role("button", name="Markdown", exact=True).click()
                expect(page.get_by_text("Format confirmed.", exact=True)).to_be_visible()
                expect(wait).to_have_count(0)
                expect(top.get_by_role("img", name="Idle")).to_be_visible()
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
