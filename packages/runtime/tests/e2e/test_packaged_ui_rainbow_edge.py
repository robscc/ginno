"""Only the running session's sidebar row animates during a packaged turn."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_packaged_ui_tasks_overview as harness
from test_packaged_ui_transcript_event_rows import capture_socket, wait_for_session_socket

pytestmark = pytest.mark.e2e

PROMPT = "Check the running session"
ANSWER = "Running session finished."


@pytest.fixture
def model_gateway():
    harness._require_runtime()
    started = threading.Event()
    release = threading.Event()

    class Gateway(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            if PROMPT in json.dumps(request["messages"]):
                started.set()
                if not release.wait(20):
                    self.send_error(504)
                    return
            chunk = {"id": "chatcmpl_rainbow", "object": "chat.completion.chunk", "created": 1, "model": request["model"]}
            events = [
                dict(chunk, choices=[{"index": 0, "delta": {"role": "assistant", "content": ANSWER}, "finish_reason": None}]),
                dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            ]
            body = ("".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", started, release
        finally:
            release.set()
            server.shutdown()
            thread.join()


def test_packaged_ui_running_rainbow_edge(tmp_path, monkeypatch, model_gateway):
    from playwright.sync_api import expect, sync_playwright

    base_url, started, release = model_gateway
    settings = harness._settings()
    settings["providers"]["custom"].update(base_url=base_url, default_model="m", timeout_s=30)
    monkeypatch.setattr(harness, "_settings", lambda: settings)
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port, fake_llm=False)
    try:
        harness._wait_health(port)
        title = "Rainbow Edge E2E"
        idle_title = "Idle Edge E2E"
        sessions = [harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": name,
            "provider": "custom", "model": "m",
        }) for name in (title, idle_title)]
        sid = sessions[0]["id"]

        with sync_playwright() as pw:
            browser, page = harness._new_page(pw)
            try:
                page.emulate_media(reduced_motion="no-preference")
                capture_socket(page)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                rows = page.locator(".nav-item")
                target = rows.filter(has=page.get_by_text(title, exact=True))
                idle = rows.filter(has=page.get_by_text(idle_title, exact=True))
                target.get_by_text(title, exact=True).click()
                wait_for_session_socket(page, sid, require_turn_state=True)
                edge = target.locator(".running-rainbow-edge")
                expect(idle).to_be_visible()
                expect(edge).to_have_count(0)
                composer = page.locator("textarea")
                composer.fill(PROMPT)
                composer.press("Enter")
                assert started.wait(10), "The chat turn never reached its configured model"
                running = harness._api(port, "GET", "/api/running")["sessions"]
                assert {session["id"] for session in running} == {sid}
                expect(edge).to_be_visible(timeout=10_000)
                expect(idle.locator(".running-rainbow-edge")).to_have_count(0)
                assert "linear-gradient" in edge.evaluate("el => getComputedStyle(el).backgroundImage")
                expect(edge).to_have_css("animation-name", "ginno-rainbow-edge")
                expect(edge).to_have_css("width", "3px")

                release.set()
                expect(page.get_by_text(ANSWER, exact=True)).to_be_visible()
                expect(edge).to_have_count(0, timeout=10_000)
                assert harness._api(port, "GET", "/api/running")["sessions"] == []
            finally:
                release.set()
                browser.close()
    finally:
        harness._stop(proc, log)
