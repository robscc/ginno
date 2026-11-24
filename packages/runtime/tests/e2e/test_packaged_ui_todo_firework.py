"""Verify TODO completion draws fireworks and undo leaves the canvas idle."""

import pytest
import test_packaged_ui_tasks_overview as smoke

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e


@pytest.fixture
def app(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        session = smoke._api(port, "POST", "/api/sessions", {
            "title": "Firework session", "project_slug": "default", "workspace": str(tmp_path),
        })
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session['id'])})")
                yield page, port, session["id"]
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


def _toggle_todo(app, title: str, *, done: bool):
    page, port, sid = app
    initial_done = not done
    created = smoke._api(port, "POST", "/api/todos", {
        "title": title, "priority": "medium", "done": initial_done, "session_ids": [sid],
    })
    assert created["ok"] and created["todo"]["done"] is initial_done, created
    todo_id = created["todo"]["id"]

    label_before, label_after = "Mark as done", "Mark as not done"
    if not done:
        label_before, label_after = label_after, label_before
    base = f"http://127.0.0.1:{port}"
    page.goto(base + "/", wait_until="load")
    page.get_by_role("heading", name="Firework session", exact=True).wait_for()
    page.get_by_role("tab", name="TODO", exact=True).click()
    row = page.locator(".group").filter(has=page.get_by_text(title, exact=True))
    url = f"{base}/api/todos/{todo_id}"
    with page.expect_response(lambda r: r.url == url and r.request.method == "PATCH") as patched:
        row.get_by_title(label_before, exact=True).click()
    response = patched.value
    assert response.request.post_data_json == {"done": done}
    assert response.status == 200
    result = response.json()
    assert result["ok"] and result["todo"]["done"] is done, result
    row.get_by_title(label_after, exact=True).wait_for()
    return page


def test_packaged_ui_todo_check_fires_firework(app):
    page = _toggle_todo(app, "firework check", done=True)
    page.locator("body > canvas").wait_for(state="attached")
    # The renderer updates the count after painting each frame.
    page.wait_for_function("() => (window.__fwParts ?? 0) > 0", timeout=5_000)


def test_packaged_ui_todo_uncheck_no_firework(app):
    page = _toggle_todo(app, "no firework", done=False)
    assert page.evaluate("() => window.__fwParts ?? 0") == 0
    assert page.locator("body > canvas").count() == 0
