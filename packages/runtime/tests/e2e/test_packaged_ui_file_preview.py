"""A preview response may update only the viewer selection that requested it."""

from __future__ import annotations

import json
import re

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


@pytest.fixture
def preview_page(tmp_path, record_property):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    port = smoke._free_port()
    assert port != 8787
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        health = smoke._api(port, "GET", "/api/health")
        record_property("runtime_fingerprint", health["build"]["fingerprint"])
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "title": "Preview race fixture", "workspace": str(tmp_path),
        })
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session['id'])})")
                page.goto(f"http://127.0.0.1:{port}", wait_until="load")
                expect(page.get_by_role("heading", name="Preview race fixture", exact=True)).to_be_visible()
                yield page
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


def _attach(page, paths):
    from playwright.sync_api import expect

    composer = page.locator("textarea").first
    page.locator('input[type="file"]').set_input_files(paths)
    for path in paths:
        expect(page.get_by_role("button", name=f"Remove {path.name}", exact=True)).to_be_visible()
    expect(page.get_by_text("Uploading…", exact=True)).to_have_count(0)
    composer.fill("Compare these local previews.")
    composer.press("Enter")
    chips = [page.get_by_role("button", name=path.name, exact=True) for path in paths]
    for chip in chips:
        expect(chip).to_be_visible()
    return chips


def _viewer(page):
    return page.locator("div.fixed.inset-0").filter(has=page.get_by_title("Download original file", exact=True))


def _install_response_gate(page):
    # Fetch and parse the real API response, then pause delivery to the app.
    # The optional rejection is applied only after the real response arrives.
    page.evaluate("""() => {
        const fetch = window.fetch.bind(window);
        const gate = window.previewGate = {requests: [], hold: false, missing: []};
        window.addEventListener('ginno:file-missing', event => gate.missing.push(event.detail.fileId));
        window.fetch = async (input, init) => {
            const url = new URL(input instanceof Request ? input.url : input, location.href);
            if (!/^\\/api\\/files\\/[^/]+\\/preview$/.test(url.pathname)) return fetch(input, init);
            const request = {path: url.pathname, sheet: url.searchParams.get('sheet'),
                offset: url.searchParams.get('offset'), status: null, release: null};
            gate.requests.push(request);
            const held = gate.hold;
            const response = await fetch(input, init);
            const body = await response.json();
            request.status = response.status;
            const reject = held && await new Promise(resolve => { request.release = resolve; });
            if (reject) throw new TypeError('Injected preview delivery failure');
            return {json: async () => body};
        };
    }""")


def _held_response(page, index, status=200):
    page.wait_for_function("index => typeof window.previewGate.requests[index]?.release === 'function'", arg=index)
    assert page.evaluate("index => window.previewGate.requests[index].status", index) == status


def _release(page, index, *, reject=False):
    # Give fetch/json continuations and their resulting React render a turn.
    page.evaluate("""async ({index, reject}) => {
        window.previewGate.requests[index].release(reject);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""", {"index": index, "reject": reject})


def _record_commits(viewer, marker):
    viewer.evaluate("""(node, marker) => {
        const gate = window.previewGate;
        gate.staleCommits = 0;
        new MutationObserver(mutations => {
            for (const mutation of mutations) {
                for (const added of mutation.addedNodes) {
                    if (added.textContent.includes(marker)) gate.staleCommits++;
                }
                if (mutation.type === 'characterData' && mutation.target.textContent.includes(marker)) {
                    gate.staleCommits++;
                }
            }
        }).observe(node, {subtree: true, childList: true, characterData: true});
    }""", marker)


@pytest.mark.parametrize("first,second,hold_current", [
    pytest.param("A", "B", False, id="A-to-B"),
    pytest.param("B", "A", False, id="B-to-A"),
    pytest.param("A", "B", True, id="pending-current"),
])
def test_closed_file_cannot_overwrite_current_document(preview_page, tmp_path, record_property, first, second, hold_current):
    from playwright.sync_api import expect

    page = preview_page
    paths = [tmp_path / f"Document-{label}.md" for label in (first, second)]
    for path, label in zip(paths, (first, second), strict=True):
        path.write_text(f"# Preview body {label}\n", encoding="utf-8")
    old_chip, new_chip = _attach(page, paths)
    _install_response_gate(page)
    page.evaluate("window.previewGate.hold = true")
    old_chip.click()
    _held_response(page, 0)
    viewer = _viewer(page)
    viewer.get_by_title("Close", exact=True).click()
    page.evaluate("hold => { window.previewGate.hold = hold; }", hold_current)
    new_chip.click()
    title = viewer.get_by_text(paths[1].name, exact=True)
    body = viewer.get_by_role("heading", name=f"Preview body {second}", exact=True)
    expect(title).to_be_visible()
    loading = viewer.get_by_text("Loading…", exact=True)
    spinner = viewer.get_by_title("Refresh", exact=True).locator("svg")
    if hold_current:
        _held_response(page, 1)
        expect(loading).to_be_visible()
    else:
        expect(body).to_be_visible()
    _record_commits(viewer, f"Preview body {first}")
    _release(page, 0)
    commits = page.evaluate("window.previewGate.staleCommits")
    record_property("stale_body_commits", commits)
    assert commits == 0, {"title": title.text_content(), "headings": viewer.get_by_role("heading").all_text_contents()}
    expect(title).to_be_visible()
    if hold_current:
        expect(loading).to_be_visible()
        expect(spinner).to_have_class(re.compile(r"\banimate-spin\b"))
        _release(page, 1)
    expect(body).to_be_visible()
    paths = page.evaluate("window.previewGate.requests.map(request => request.path)")
    assert len(paths) == 2 and paths[0] != paths[1]


@pytest.mark.parametrize("reject", [False, True], ids=["missing", "rejected"])
def test_old_refresh_cannot_set_error_or_finish_current_loading(preview_page, tmp_path, record_property, reject):
    from playwright.sync_api import expect

    page = preview_page
    path = tmp_path / "Refresh.md"
    path.write_text("# Latest refresh is readable\n", encoding="utf-8")
    (chip,) = _attach(page, [path])
    _install_response_gate(page)
    chip.click()
    viewer = _viewer(page)
    expect(viewer.get_by_role("heading", name="Latest refresh is readable")).to_be_visible()
    refresh = viewer.get_by_title("Refresh", exact=True)
    # Temporarily make the actual uploaded fixture unavailable to get a real 404.
    session_files = list(tmp_path.rglob(f"*{path.name}"))
    (uploaded,) = [item for item in session_files if item != path]
    backup = uploaded.with_suffix(".held")
    uploaded.rename(backup)
    try:
        page.evaluate("window.previewGate.hold = true")
        refresh.click()
        _held_response(page, 1, status=404)
    finally:
        backup.rename(uploaded)
    refresh.click()
    _held_response(page, 2)
    expect(viewer.get_by_text("Loading…", exact=True)).to_be_visible()
    expect(refresh.locator("svg")).to_have_class(re.compile(r"\banimate-spin\b"))
    stale_marker = "Cannot connect to the runtime" if reject else "File deleted"
    _record_commits(viewer, stale_marker)
    _release(page, 1, reject=reject)
    commits = page.evaluate("window.previewGate.staleCommits")
    record_property("stale_error_commits", commits)
    assert commits == 0
    missing_events = page.evaluate("window.previewGate.missing")
    record_property("stale_missing_events", len(missing_events))
    assert missing_events == []
    expect(refresh.locator("svg")).to_have_class(re.compile(r"\banimate-spin\b"))
    expect(viewer.get_by_text("Loading…", exact=True)).to_be_visible()
    _release(page, 2)
    expect(viewer.get_by_role("heading", name="Latest refresh is readable")).to_be_visible()
    assert page.evaluate("window.previewGate.requests.length") == 3


def test_rapid_pages_sheet_and_file_switch(preview_page, tmp_path, record_property):
    from openpyxl import Workbook
    from playwright.sync_api import expect

    page = preview_page
    books = [tmp_path / name for name in ("First.xlsx", "Second.xlsx")]
    for path, names in zip(books, [("Default", "Alternate"), ("Other default", "Alternate")], strict=True):
        workbook = Workbook()
        workbook.active.title = names[0]
        workbook.create_sheet(names[1])
        for sheet in workbook:
            sheet.append(["Item"])
            for row in range(1, 206):
                sheet.append([f"{path.stem} {sheet.title} row {row}"])
        workbook.save(path)

    first, second = _attach(page, books)
    viewer = _viewer(page)
    # Spreadsheet uploads auto-open a preview. Wait for the cold extractor
    # to render once, then close the automatic preview before counting reads.
    expect(viewer.get_by_role("table")).to_be_visible(timeout=20_000)
    viewer.get_by_title("Close", exact=True).click()
    _install_response_gate(page)
    first.click()
    expect(viewer.get_by_role("cell", name="First Default row 1", exact=True)).to_be_visible()
    next_page = viewer.get_by_role("button", name="Next")
    page.evaluate("window.previewGate.hold = true")
    next_page.click()
    _held_response(page, 1)
    next_page.click()
    _held_response(page, 2)
    _release(page, 2)
    expect(viewer.get_by_role("cell", name="First Default row 201", exact=True)).to_be_visible()
    _record_commits(viewer, "First Default row 101")
    _release(page, 1)
    commits = page.evaluate("window.previewGate.staleCommits")
    record_property("stale_page_commits", commits)
    assert commits == 0
    expect(viewer.get_by_role("cell", name="First Default row 201", exact=True)).to_be_visible()

    viewer.get_by_role("button", name="Previous").click()
    _held_response(page, 3)
    viewer.get_by_role("button", name="Alternate", exact=False).click()
    _held_response(page, 4)
    _release(page, 4)
    expect(viewer.get_by_role("cell", name="First Alternate row 1", exact=True)).to_be_visible()
    _record_commits(viewer, "First Default row 101")
    _release(page, 3)
    commits = page.evaluate("window.previewGate.staleCommits")
    record_property("stale_sheet_commits", commits)
    assert commits == 0
    expect(viewer.get_by_role("cell", name="First Alternate row 1", exact=True)).to_be_visible()

    next_page.click()
    _held_response(page, 5)
    _release(page, 5)
    expect(viewer.get_by_role("cell", name="First Alternate row 101", exact=True)).to_be_visible()
    viewer.get_by_title("Close", exact=True).click()
    second.click()
    _held_response(page, 6)
    _release(page, 6)
    expect(viewer.get_by_text("Second.xlsx", exact=True)).to_be_visible()
    expect(viewer.get_by_role("cell", name="Second Other default row 1", exact=True)).to_be_visible()
    assert page.evaluate("""() => window.previewGate.requests.map(
        ({sheet, offset}) => [sheet, Number(offset)]
    )""") == [[None, 0], [None, 100], [None, 200], [None, 100], ["Alternate", 0], ["Alternate", 100], [None, 0]]
    paths = page.evaluate("window.previewGate.requests.map(request => request.path)")
    assert paths[:6] == [paths[0]] * 6
    assert paths[-1] != paths[0]
