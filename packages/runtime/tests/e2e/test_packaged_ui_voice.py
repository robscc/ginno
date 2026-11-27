"""Exercise the real browser microphone pipeline with a synthetic microphone stream."""

import json

import pytest
from playwright.sync_api import expect, sync_playwright
import test_packaged_ui_tasks_overview as harness
from test_packaged_ui_transcript_event_rows import capture_socket, open_session, wait_for_session_socket

pytestmark = pytest.mark.e2e


@pytest.fixture
def voice_session(tmp_path):
    harness._require_runtime()
    port = harness._free_port()
    proc, log = harness._spawn(tmp_path, port)
    try:
        harness._wait_health(port)
        session = harness._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Voice dictation",
        })
        yield port, session["id"]
    finally:
        harness._stop(proc, log)


def test_voice_dictation_keeps_draft_until_finished(voice_session):
    port, sid = voice_session
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(permissions=["microphone"])
        page.add_init_script("""navigator.mediaDevices.getUserMedia = async () => {
            const context = new AudioContext();
            const oscillator = context.createOscillator();
            const destination = context.createMediaStreamDestination();
            oscillator.connect(destination);
            oscillator.start();
            await context.resume();
            return destination.stream;
        }""")
        page.add_init_script("localStorage.setItem('ginno.language', 'en')")
        page.route("**/api/voice/settings", lambda route: route.fulfill(json={
            "selected": "test", "shortcut": "ctrl-alt-v", "models": [{"id": "test"}],
        }))
        audio = []

        def provider(socket):
            first_frame = True

            def message(frame):
                nonlocal first_frame
                if isinstance(frame, bytes):
                    audio.append(frame)
                    if first_frame:
                        first_frame = False
                        socket.send(json.dumps({"type": "partial", "id": 1, "text": "preview"}))
                elif frame == "stop":
                    socket.send(json.dumps({"type": "final", "id": 1, "text": "voice result"}))
                    socket.send(json.dumps({"type": "done"}))
                else:
                    assert json.loads(frame)["session_id"] == sid
                    socket.send(json.dumps({"type": "ready"}))
            socket.on_message(message)

        page.route_web_socket("**/api/voice/dictation/ws", provider)
        capture_socket(page)
        open_session(page, port, sid)
        wait_for_session_socket(page, sid)
        draft = page.locator("textarea").first
        draft.fill("Keep original")
        page.get_by_role("button", name="Start dictation").click()
        expect(page.get_by_text("preview", exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
        expect(draft).to_have_value("Keep original")
        page.get_by_role("button", name="Finish", exact=True).click()
        expect(draft).to_have_value("Keep original voice result")
        assert audio and all(0 < len(frame) <= 3200 and len(frame) % 2 == 0 for frame in audio)
        draft.focus()
        page.keyboard.down("Control")
        page.keyboard.down("Alt")
        page.keyboard.down("KeyV")
        expect(page.get_by_text("preview", exact=True)).to_be_visible()
        page.keyboard.up("KeyV")
        page.keyboard.up("Alt")
        page.keyboard.up("Control")
        expect(draft).to_have_value("Keep original voice result voice result")
        browser.close()
