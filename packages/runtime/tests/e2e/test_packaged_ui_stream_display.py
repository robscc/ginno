"""The chat transcript commits bursty live frames without delaying interaction boundaries."""

import pytest
import test_packaged_ui_tasks_overview as harness
from playwright.sync_api import expect
from test_packaged_ui_transcript_event_rows import (
    app as app,
    capture_socket,
    open_session,
    send_frame,
    wait_for_session_socket,
)

pytestmark = pytest.mark.e2e


def test_stream_frames_and_display_boundaries(app, tmp_path):
    page, port, sid = app
    second = harness._api(
        port, "POST", "/api/sessions",
        {"project_slug": "default", "workspace": str(tmp_path), "title": "Second stream display session"},
    )["id"]
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    send_frame(page, sid, "turn.start", turn_id="display-turn", agent_id="dev")

    send_frame(page, sid, "tool.start", turn_id="display-turn", id="display-tool", name="bash")
    send_frame(page, sid, "tool.args_delta", turn_id="display-turn", id="display-tool", tail="pending argument")
    process = page.locator("[data-process-active]")
    toggle = process.locator(":scope > button")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    toggle.click()
    expect(process.get_by_text("· pending argument", exact=True)).to_be_visible()
    send_frame(page, sid, "tool.args", turn_id="display-turn", id="display-tool", preview="final argument")
    expect(process.get_by_text("· final argument", exact=True)).to_be_visible()
    send_frame(page, sid, "tool.end", turn_id="display-turn", id="display-tool", name="bash", content="ok")

    chunks = [f"chunk-{i:02d} " for i in range(50)]
    body = "".join(chunks).strip()
    count = page.evaluate('''([sid, chunks]) => {
        const ws = window.__transcriptTestSockets.find(ws => new URL(ws.url).pathname.endsWith('/' + sid));
        const request = window.requestAnimationFrame;
        const cancel = window.cancelAnimationFrame;
        const held = new Map();
        let id = 0;
        window.requestAnimationFrame = (callback) => { held.set(--id, callback); return id; };
        window.cancelAnimationFrame = (handle) => handle < 0 ? held.delete(handle) : cancel(handle);
        window.__releaseDisplayFrames = () => {
            window.requestAnimationFrame = request;
            window.cancelAnimationFrame = cancel;
            for (const callback of held.values()) callback(performance.now());
        };
        for (const content of chunks) ws.dispatchEvent(new MessageEvent('message', {
            data: JSON.stringify({event: 'token.delta', turn_id: 'display-turn', content}),
        }));
        return held.size;
    }''', [sid, chunks])
    assert count == 1
    expect(page.get_by_text(body, exact=True)).to_have_count(0)
    page.evaluate("window.__releaseDisplayFrames()")
    expect(page.get_by_text(body, exact=True)).to_be_visible()

    # With frame delivery suspended, permission and completion must each
    # publish the preceding text in the same display commit.
    page.evaluate("window.__realDisplayRaf = window.requestAnimationFrame; window.requestAnimationFrame = () => -1")
    send_frame(page, sid, "token.delta", turn_id="display-turn", content="before permission ")
    send_frame(page, sid, "permission.request", tool="display-permission-sentinel", args={})
    expect(page.get_by_text("display-permission-sentinel", exact=True)).to_be_visible()
    expect(page.get_by_text(f"{body} before permission", exact=True)).to_be_visible()
    send_frame(page, sid, "token.delta", turn_id="display-turn", content="before finish")
    send_frame(page, sid, "message.end", turn_id="display-turn", origin="schedule")
    finished = f"{body} before permission before finish"
    expect(page.get_by_text(finished, exact=True)).to_be_visible()

    # Simulate the browser pausing animation frames when the tab is hidden.
    send_frame(page, sid, "turn.start", turn_id="hidden-turn", agent_id="dev")
    send_frame(page, sid, "token.delta", turn_id="hidden-turn", content="visible-to-hidden ")
    page.evaluate('''() => {
        Object.defineProperty(document, 'hidden', {configurable: true, value: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }''')
    expect(page.get_by_text("visible-to-hidden", exact=True)).to_be_visible()
    send_frame(page, sid, "token.delta", turn_id="hidden-turn", content="still streaming")
    expect(page.get_by_text("visible-to-hidden still streaming", exact=True)).to_be_visible()
    page.evaluate("delete document.hidden")

    # Capture a late frame even after cancel, then return to the old session
    # to confirm the reducer kept the background text in its own store.
    page.evaluate('''sid => {
        const ws = window.__transcriptTestSockets.find(ws => new URL(ws.url).pathname.endsWith('/' + sid));
        window.requestAnimationFrame = (callback) => { window.__lateDisplayFrame = callback; return -1; };
        ws.dispatchEvent(new MessageEvent('message', {data: JSON.stringify({
            event: 'token.delta', turn_id: 'hidden-turn', content: ' old-session-only',
        })}));
        window.requestAnimationFrame = window.__realDisplayRaf;
    }''', sid)
    page.evaluate("window.__ginnoOpenSession", second)
    wait_for_session_socket(page, second, require_turn_state=True)
    send_frame(page, second, "turn.start", turn_id="second-turn", agent_id="dev", text="second-session-only")
    expect(page.get_by_text("second-session-only", exact=True)).to_be_visible()
    page.evaluate("window.__lateDisplayFrame(performance.now())")
    expect(page.get_by_text("old-session-only", exact=False)).to_have_count(0)
    page.evaluate("window.__ginnoOpenSession", sid)
    expect(page.get_by_text("visible-to-hidden still streaming old-session-only", exact=True)).to_be_visible()
    expect(page.get_by_text("second-session-only", exact=True)).to_have_count(0)

    # A real socket close commits the pending text even with frames paused.
    page.evaluate('''sid => {
        window.requestAnimationFrame = () => -1;
        const ws = window.__transcriptTestSockets.find(ws => new URL(ws.url).pathname.endsWith('/' + sid));
        ws.dispatchEvent(new MessageEvent('message', {data: JSON.stringify({
            event: 'token.delta', turn_id: 'hidden-turn', content: ' before disconnect',
        })}));
        ws.close();
    }''', sid)
    expect(page.get_by_text(
        "visible-to-hidden still streaming old-session-only before disconnect", exact=True,
    )).to_be_visible()
