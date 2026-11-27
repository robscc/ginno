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
        page = browser.new_context(permissions=["microphone"]).new_page()
        page.add_init_script("""navigator.permissions.query = async () => { throw new TypeError('Unsupported permission query'); };
        navigator.mediaDevices.getUserMedia = async () => {
            const context = new AudioContext();
            const oscillator = context.createOscillator();
            const destination = context.createMediaStreamDestination();
            oscillator.connect(destination);
            oscillator.start();
            await context.resume();
            return destination.stream;
        }""")
        page.add_init_script("localStorage.setItem('ginno.language', 'en')")
        voice_settings = {
            "selected": "test", "shortcut": "fn", "models": [{
                "id": "test", "name": "Test ASR", "model": "qwen3-asr-flash-realtime", "region": "",
            }],
        }

        def settings_route(route):
            if route.request.method == "PUT":
                voice_settings.update(route.request.post_data_json)
                route.fulfill(json={"ok": True})
            else:
                route.fulfill(json=voice_settings)

        page.context.route("**/api/voice/settings", settings_route)
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
        draft.focus()
        # Simulate a settings change in another window while the chat stays mounted.
        settings = page.context.new_page()
        settings.goto(f"http://127.0.0.1:{port}/settings/connections/voice")
        settings.get_by_role("combobox", name="Hold to talk").select_option("ctrl-alt-v")
        settings.get_by_role("button", name="Save", exact=True).click()
        expect(settings.get_by_text("Saved", exact=True)).to_be_visible()
        settings.close()
        draft.focus()
        page.keyboard.down("Control")
        page.keyboard.down("Alt")
        page.keyboard.down("KeyV")
        expect(page.get_by_text("preview", exact=True)).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_disabled()
        expect(draft).to_have_value("Keep original")
        page.keyboard.up("Alt")
        page.keyboard.up("KeyV")
        page.keyboard.up("Control")
        expect(draft).to_have_value("Keep original voice result")
        assert audio and all(0 < len(frame) <= 3200 and len(frame) % 2 == 0 for frame in audio)
        page.get_by_role("button", name="Start dictation").click()
        expect(page.get_by_text("preview", exact=True)).to_be_visible()
        page.get_by_role("button", name="Finish", exact=True).click()
        expect(draft).to_have_value("Keep original voice result voice result")
        browser.close()


def test_voice_settings_explains_unavailable_model(voice_session):
    port, _ = voice_session
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.route("**/api/voice/settings", lambda route: route.fulfill(json={
            "selected": "", "shortcut": "fn", "models": [],
            "unavailable": [{"id": "file", "name": "File ASR", "models": ["qwen-audio-3.0-asr-flash"],
                             "reason": "unsupported_model"}],
        }))
        page.goto(f"http://127.0.0.1:{port}/settings/connections/voice")
        expect(page.get_by_text("File ASR · qwen-audio-3.0-asr-flash", exact=True)).to_be_visible()
        expect(page.get_by_text("Ginno currently supports qwen3-asr-flash-realtime for real-time dictation.",
                                exact=True)).to_be_visible()
        expect(page.get_by_role("combobox", name="Speech recognition model")).to_have_value("")
        browser.close()
