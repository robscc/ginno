"""Operation accents follow the transcript and its motion preferences."""

import pytest
from playwright.sync_api import expect
from test_packaged_ui_transcript_event_rows import (
    app as app,
    capture_socket,
    open_session,
    send_frame,
    wait_for_session_socket,
)

pytestmark = pytest.mark.e2e


def _open_transcript(app):
    page, port, sid = app
    page.emulate_media(reduced_motion="no-preference", color_scheme="dark")
    capture_socket(page)
    open_session(page, port, sid)
    wait_for_session_socket(page, sid, require_turn_state=True)
    return page, port, sid


def _expect_edge(card, state, color, *, animated=False):
    expect(card).to_be_visible()
    expect(card).to_have_attribute("data-operation-state", state)
    edge = card.evaluate("""el => {
        const style = getComputedStyle(el, '::before');
        return [style.width, style.backgroundImage, style.animationName, style.pointerEvents];
    }""")
    assert edge[0] == "3px"
    assert edge[1].startswith("linear-gradient(") and color in edge[1], edge[1]
    assert (edge[2] != "none") is animated, edge[2]
    assert edge[3] == "none"


def test_operation_edges_follow_tool_results(app):
    page, _, sid = _open_transcript(app)
    turn = "rainbow-results"
    send_frame(page, sid, "turn.start", turn_id=turn, agent_id="dev")
    calls = [(f"rainbow-call-{i}", name) for i, name in enumerate(("read_file", "read_file", "bash"))]
    for call, name in calls:
        send_frame(page, sid, "tool.start", turn_id=turn, id=call, name=name)

    fold = page.locator("[data-process-fold]")
    title = fold.locator(":scope > button")
    expect(title).to_contain_text("Running 3 tools")
    _expect_edge(fold, "running", "rgb(241, 124, 186)", animated=True)
    positions = fold.evaluate("""async el => {
        const flow = el.getAnimations({ subtree: true }).find(a => a.animationName === 'ginno-operation-edge-flow');
        flow.pause();
        await flow.ready;
        const positions = [0, flow.effect.getTiming().duration / 2].map(time => {
            flow.currentTime = time;
            const style = getComputedStyle(el, '::before');
            return [style.backgroundPositionX, style.backgroundPositionY];
        });
        flow.play();
        return positions;
    }""")
    assert positions == [["0%", "0%"], ["0%", "100%"]], positions
    page.emulate_media(reduced_motion="reduce")
    _expect_edge(fold, "running", "rgb(241, 124, 186)")
    page.emulate_media(reduced_motion="no-preference")
    _expect_edge(fold, "running", "rgb(241, 124, 186)", animated=True)

    for call, name in calls:
        send_frame(page, sid, "tool.end", turn_id=turn, id=call, name=name, content=f"Result for {call}")
    expect(title).to_contain_text("Completed 3 actions")
    _expect_edge(fold, "completed", "rgb(117, 194, 224)")
    expect(title).to_have_attribute("aria-expanded", "false")
    title.focus()
    title.press("Enter")
    expect(title).to_have_attribute("aria-expanded", "true")
    expect(fold.get_by_text("Result for rainbow-call-0", exact=True)).to_be_visible()

    for call, content in (("failed-tool", "[error] rainbow failure"), ("stopped-tool", "(interrupted)")):
        send_frame(page, sid, "tool.start", turn_id=turn, id=call, name="bash")
        send_frame(page, sid, "tool.end", turn_id=turn, id=call, name="bash", content=content)
    failure = page.locator('[data-operation-state="failed"]').filter(has_text="rainbow failure")
    _expect_edge(failure, "failed", "rgb(255, 151, 124)")
    interrupted = page.locator('[data-operation-state="interrupted"]').filter(has_text="Interrupted")
    _expect_edge(interrupted, "interrupted", "rgb(123, 143, 174)")
    expect(fold.locator('[data-operation-state="failed"], [data-operation-state="interrupted"]')).to_have_count(0)


def test_operation_flow_can_be_paused_and_waiting_is_static(app):
    page, port, sid = _open_transcript(app)
    turn = "rainbow-permission"
    send_frame(page, sid, "turn.start", turn_id=turn, agent_id="dev")
    send_frame(page, sid, "tool.start", turn_id=turn, id="command", name="bash")
    fold = page.locator("[data-process-fold]")
    _expect_edge(fold, "running", "rgb(241, 124, 186)", animated=True)

    # Settings and chat can live in different windows of the same origin.
    with page.expect_popup() as popup:
        page.evaluate("url => { window.open(url, '_blank'); }", f"http://127.0.0.1:{port}/settings/general")
    settings = popup.value
    try:
        toggle = settings.get_by_role("checkbox", name="Animate operation cards", exact=True)
        expect(toggle).to_be_checked()
        toggle.uncheck()
        expect(page.locator("html")).to_have_attribute("data-operation-motion-paused", "")
        _expect_edge(fold, "running", "rgb(241, 124, 186)")
        settings.reload(wait_until="load")
        expect(toggle).not_to_be_checked()
        toggle.check()
        expect(page.locator("html")).not_to_have_attribute("data-operation-motion-paused", "")
        _expect_edge(fold, "running", "rgb(241, 124, 186)", animated=True)
    finally:
        settings.close()

    send_frame(page, sid, "permission.request", turn_id=turn, tool="bash", args={"command": "pwd"})
    expect(fold.locator(":scope > button")).to_contain_text("Waiting for confirmation")
    _expect_edge(fold, "waiting", "rgb(255, 225, 164)")
    prompt = page.locator('[data-operation-state="waiting"]').filter(has_text="Permission required")
    _expect_edge(prompt, "waiting", "rgb(255, 225, 164)")
    expect(prompt.get_by_role("button", name="Allow", exact=True)).to_be_enabled()
