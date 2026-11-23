"""Verify saved tags, actual SDK traffic and cross-window capability updates."""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_packaged_ui_tasks_overview as smoke

ASTRA = "codex/gpt-6.2-astra"
SOL = "codex/gpt-6.2-sol"
PLAIN = "plain-model"
REMOVED = "unused-model"
BASE = ["auto", "low", "medium", "high"]
EXTENDED = BASE + ["xhigh", "max", "ultra"]
pytestmark = pytest.mark.e2e


def test_model_tags_save_switch_and_refresh_connected_window(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    traffic = []

    class Gateway(BaseHTTPRequestHandler):
        def write_body(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            traffic.append((self.path, {}))
            assert self.path == "/v1/models"
            payload = {
                "object": "list",
                "data": [
                    {"id": model, "object": "model"} for model in (ASTRA, SOL, PLAIN, REMOVED)
                ],
            }
            self.write_body(json.dumps(payload).encode(), "application/json")

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            traffic.append((self.path, request))
            assert self.path == "/v1/chat/completions"
            chunk = {
                "id": "chatcmpl_model_tags",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": request["model"],
            }
            answer = "Gateway answered " + request["model"]
            events = [
                dict(
                    chunk,
                    choices=[
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": answer},
                            "finish_reason": None,
                        }
                    ],
                ),
                dict(chunk, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            ]
            body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            self.write_body((body + "data: [DONE]\n\n").encode(), "text/event-stream")

    with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as gateway:
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        try:
            settings = smoke._settings()
            settings["providers"]["custom"].update(
                name="Model Tag Gateway",
                model=ASTRA,
                default_model=ASTRA,
                models=[ASTRA, SOL, PLAIN, REMOVED],
                base_url=f"http://127.0.0.1:{gateway.server_port}/v1",
            )
            monkeypatch.setattr(smoke, "_settings", lambda: settings)
            port = smoke._free_port()
            proc, log = smoke._spawn(tmp_path, port, fake_llm=False)
            try:
                smoke._wait_health(port)
                url = f"http://127.0.0.1:{port}"
                sid = smoke._api(
                    port,
                    "POST",
                    "/api/sessions",
                    {
                        "project_slug": "default",
                        "workspace": str(tmp_path),
                        "title": "Model tag verification",
                        "provider": "custom",
                        "model": ASTRA,
                    },
                )["id"]
                with sync_playwright() as playwright:
                    browser, page = smoke._new_page(playwright)
                    chat_url = f"{url}/"
                    page.add_init_script(
                        f'localStorage.setItem("ginno-last-session", {json.dumps(sid)})'
                    )
                    page.goto(chat_url)
                    menu = page.locator('button[title*="Switch this session"]').first
                    options = page.locator("[data-thinking-effort]")

                    def show_efforts(model, expected):
                        if not page.locator("[data-model-efforts-toggle]").count():
                            menu.click()
                        toggle = page.get_by_role(
                            "button", name=f"Thinking for {model}", exact=True
                        )
                        if toggle.get_attribute("aria-expanded") != "true":
                            toggle.click()
                        expect(options).to_have_count(len(expected))
                        assert (
                            options.evaluate_all("(els) => els.map(e => e.dataset.thinkingEffort)")
                            == expected
                        )

                    def settings_page():
                        page.goto(url + "/settings/connections/model-api")
                        page.get_by_text("Model Tag Gateway", exact=True).click()
                        expect(page.locator("[data-model-tag-picker]").first).to_be_visible()

                    def tag_menu(model):
                        page.locator(
                            f'[data-model-tag-picker][aria-label^="Model tag for {model}:"]'
                        ).click()
                        return page.get_by_role("menuitemradio")

                    def save():
                        with page.expect_response(
                            lambda response: response.url.endswith("/api/model_configs/verify")
                        ) as verified:
                            page.get_by_role(
                                "button", name=re.compile("Verify.*save", re.I)
                            ).click()
                        data = verified.value.json()
                        assert data["ok"], data
                        return data["config"]

                    def effort(value):
                        return page.locator(f'[data-thinking-effort="{value}"]')

                    def send(prompt, model):
                        composer = page.locator("textarea").first
                        expect(composer).to_be_visible()
                        composer.fill(prompt)
                        composer.press("Enter")
                        expect(
                            page.get_by_text("Gateway answered " + model, exact=True).last
                        ).to_be_visible(timeout=15000)
                        return [body for path, body in traffic if path == "/v1/chat/completions"][
                            -1
                        ]

                    expect(menu).to_contain_text(ASTRA)
                    show_efforts(ASTRA, BASE)
                    show_efforts(PLAIN, BASE)
                    settings_page()
                    for model in (ASTRA, SOL):
                        items = tag_menu(model)
                        expect(items.filter(has_text="Default")).to_have_attribute(
                            "aria-checked", "true"
                        )
                        expect(items.filter(has_text="Default")).not_to_contain_text("Recommended")
                        expect(items.filter(has_text="Codex")).to_contain_text("Recommended")
                        items.filter(has_text="Codex").click()
                    items = tag_menu(PLAIN)
                    expect(items.filter(has_text="Codex")).not_to_contain_text("Recommended")
                    page.keyboard.press("Escape")
                    expect(
                        page.get_by_role("radio", name=f"Set {ASTRA} as the default model")
                    ).to_be_checked()
                    page.get_by_role("button", name=f"Remove {REMOVED}").click()
                    expect(page.get_by_role("button", name=f"Remove {REMOVED}")).to_have_count(0)
                    config = save()
                    assert REMOVED not in config["models"]
                    assert config["default_model"] == ASTRA
                    assert {
                        model: tag["max_effort"] for model, tag in config["model_tags"].items()
                    } == {ASTRA: "ultra", SOL: "ultra"}
                    assert any(path == "/v1/models" for path, _ in traffic)

                    page.goto(chat_url)
                    show_efforts(SOL, EXTENDED)
                    effort("ultra").click()
                    expect(menu).to_contain_text("ULTRA")
                    codex_body = send("run as Codex", SOL)
                    assert codex_body["reasoning_effort"] == "ultra"
                    assert all(
                        key not in codex_body
                        for key in ("temperature", "enable_search", "enable_thinking")
                    )
                    show_efforts(ASTRA, EXTENDED)
                    effort("high").click()
                    expect(menu).to_contain_text("HI")
                    menu.click()
                    page.get_by_role("button", name=PLAIN, exact=True).click()
                    expect(menu).to_contain_text(PLAIN)
                    expect(
                        page.get_by_text("Thinking effort returned to Auto", exact=True)
                    ).to_be_visible()
                    show_efforts(PLAIN, BASE)
                    expect(effort("auto")).to_have_attribute("aria-pressed", "true")
                    page.locator("div.fixed.inset-0.z-40").click(position={"x": 5, "y": 5})
                    plain_body = send("run as default", PLAIN)
                    assert "reasoning_effort" not in plain_body
                    assert any(
                        "run as Codex" in str(msg.get("content")) for msg in plain_body["messages"]
                    )

                    show_efforts(ASTRA, EXTENDED)
                    effort("ultra").click()
                    expect(menu).to_contain_text("ULTRA")
                    other = browser.new_context().new_page()
                    other.add_init_script(
                        f'localStorage.setItem("ginno-last-session", {json.dumps(sid)});'
                        'localStorage.setItem("ginno.language", "en");'
                    )
                    other.goto(chat_url)
                    other_chip = other.locator('button[title*="Switch this session"]').first
                    expect(other_chip).to_contain_text("ULTRA")
                    other_chip.click()
                    other.get_by_role("button", name=f"Thinking for {ASTRA}", exact=True).click()
                    expect(other.locator("[data-thinking-effort]")).to_have_count(len(EXTENDED))
                    settings_page()
                    tag_menu(ASTRA).filter(has_text="Default").click()
                    assert ASTRA not in save()["model_tags"]
                    expect(
                        other.get_by_text("Thinking effort returned to Auto", exact=True)
                    ).to_be_visible()
                    expect(other.locator("[data-thinking-effort]")).to_have_count(len(BASE))
                    page.goto(chat_url)
                    expect(menu).to_contain_text(ASTRA)
                    show_efforts(ASTRA, BASE)
                    expect(effort("auto")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator("textarea").first).to_be_visible()
                    browser.close()
            finally:
                smoke._stop(proc, log)
        finally:
            gateway.shutdown()
            worker.join()
