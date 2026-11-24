"""Load the actual highlight chunk only when Markdown renders tagged code."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

CODE = 'const payload = "original";'
CODE_RESPONSE = f"```js\n{CODE}\n```\n\n> ```python\n> print('nested')\n> ```"
PARTIAL_RESPONSE = "```js\nconst payload"
PROSE = """| Column | Value |
| --- | --- |
| Markdown | ~~deleted~~ |

- [x] GFM checklist

````
```js
This inner fence is literal text.
```
````

```plaintext
Plain tagged block.
```
"""


@pytest.mark.parametrize("chunk_fails", [False, True], ids=["highlight", "chunk-failure"])
def test_syntax_highlight_on_demand(tmp_path, monkeypatch, chunk_fails):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    chunks = [
        path.name for path in (smoke.WEB_OUT / "_next" / "static" / "chunks").rglob("*.js")
        if b"Cannot highlight as" in path.read_bytes()
    ]
    assert len(chunks) == 1, chunks
    scripts = tmp_path / "responses.json"
    scripts.write_text(json.dumps([
        {"content": PROSE},
        {"content": CODE_RESPONSE},
        {"content": "```js\nlet later = 42;\n```"},
    ]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Lazy highlighting",
        })
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(session['id'])})")
                page.context.grant_permissions(["clipboard-read", "clipboard-write"])
                requests = []
                errors = []
                hold_chunk = False
                page.on("pageerror", lambda error: errors.append(error))

                def capture(route):
                    requests.append(route)
                    if not hold_chunk:
                        route.continue_()

                deferred_frames = []
                held_socket = None

                def split_stream(ws):
                    server = ws.connect_to_server()

                    def forward(raw):
                        nonlocal held_socket
                        if held_socket is ws:
                            deferred_frames.append(raw)
                            return
                        frame = json.loads(raw)
                        if frame.get("event") == "token.delta" and frame.get("content") == CODE_RESPONSE:
                            held_socket = ws
                            deferred_frames.append(json.dumps({
                                **frame, "content": CODE_RESPONSE.removeprefix(PARTIAL_RESPONSE),
                            }))
                            ws.send(json.dumps({**frame, "content": PARTIAL_RESPONSE}))
                        else:
                            ws.send(raw)

                    server.on_message(forward)

                page.route_web_socket("**/api/ws/sessions/**", split_stream)
                page.route(f"**/{chunks[0]}", capture)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                expect(page.get_by_role("heading", name="Lazy highlighting", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="Connection status: Connected")).to_be_visible()
                composer = page.locator("textarea").first

                def send(text):
                    composer.fill(text)
                    composer.press("Enter")

                send("Show prose and literal code.")
                expect(page.get_by_role("cell", name="Markdown")).to_be_visible()
                expect(page.locator("del")).to_have_text("deleted")
                expect(page.get_by_role("checkbox")).to_be_checked()
                expect(page.get_by_text("Plain tagged block.", exact=True)).to_be_visible()
                assert len(requests) == 0, "prose and plain code must not request the highlighter"

                def is_highlight_request(req):
                    return urlsplit(req.url).path.endswith(f"/{chunks[0]}")

                hold_chunk = True
                with page.expect_request(is_highlight_request):
                    send("Show two highlighted blocks.")
                expect(page.locator("pre > code.language-js")).to_have_text("const payload")
                assert held_socket is not None
                held_socket.send(deferred_frames.pop(0))
                js = page.locator("pre > code.language-js").filter(has_text=CODE)
                python = page.locator("pre > code.language-python")
                expect(js).to_be_visible()
                expect(python).to_be_visible()
                # Keep the import pending until both plain code blocks are visible.
                assert len(requests) == 1
                expect(js.locator(".hljs-keyword")).to_have_count(0)
                if chunk_fails:
                    with page.expect_event("requestfailed", predicate=is_highlight_request):
                        requests[0].abort()
                else:
                    requests[0].continue_()
                    keyword = js.locator(".hljs-keyword")
                    expect(keyword).to_have_text("const")
                    assert keyword.evaluate("node => getComputedStyle(node).color !== getComputedStyle(node.closest('code')).color")
                    expect(python.locator(".hljs-string")).to_have_text("'nested'")

                finished_socket, held_socket = held_socket, None
                for raw in deferred_frames:
                    finished_socket.send(raw)
                deferred_frames.clear()

                copy = js.locator("../..").get_by_role("button", name="Copy", exact=True)
                copy.click()
                expect(js.locator("../..").get_by_role("button", name="Copied ✓", exact=True)).to_be_visible()
                assert page.evaluate("navigator.clipboard.readText()") == CODE

                send("Show one more code block.")
                later = page.locator("pre > code.language-js").filter(has_text="let later = 42;")
                expect(later).to_be_visible()
                if chunk_fails:
                    expect(later.locator("span")).to_have_count(0)
                else:
                    expect(later.locator(".hljs-keyword")).to_have_text("let")
                assert len(requests) == 1, "every block in the document must share one import"
                assert not errors

                if not chunk_fails:
                    hold_chunk = False
                    page.reload(wait_until="load")
                    expect(js.locator(".hljs-keyword")).to_have_text("const")
                    expect(later.locator(".hljs-keyword")).to_have_text("let")
                    assert len(requests) == 2, "the replay document must load the same chunk once"
                    assert not errors
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
