"""Verify the Responses image switch through chat, the Images API and replay."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

CHAT_MODEL = "codex/fixture-chat"
IMAGE_MODEL = "codex/gpt-image-2"
PROMPT = "Draw a blue circle"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


@pytest.fixture
def magpie_gateway():
    traffic = []

    class Gateway(BaseHTTPRequestHandler):
        def write_body(self, body, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            traffic.append((self.path, request))
            if self.path == "/v1/images/generations":
                self.write_body(json.dumps({
                    "created": 1, "model": IMAGE_MODEL,
                    "data": [{"b64_json": base64.b64encode(PNG).decode(), "mime_type": "image/png"}],
                }).encode())
                return
            assert self.path == "/v1/responses", self.path
            request_id = str(len(traffic))
            inputs = request["input"]
            followup = isinstance(inputs, list) and any(
                item.get("type") == "function_call_output" for item in inputs
            )
            image_tool = any(tool.get("name") == "generate_image" for tool in request.get("tools", []))
            if image_tool and request.get("stream") and PROMPT in json.dumps(inputs) and not followup:
                args = json.dumps({"prompt": "A blue circle"})
                item = {"type": "function_call", "call_id": "call_image", "id": "fc_image", "name": "generate_image"}
                events = [
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**item, "arguments": "", "status": "in_progress"}},
                    {"type": "response.function_call_arguments.delta", "output_index": 0,
                     "item_id": item["id"], "delta": args},
                ]
                item.update(status="completed", arguments=args)
            else:
                answer = "Image is ready." if followup else "Chat is ready."
                item = {"type": "message", "role": "assistant", "id": "msg_" + request_id,
                        "status": "completed", "content": [{"type": "output_text", "text": answer, "annotations": []}]}
                events = [{"type": "response.output_text.delta", "output_index": 0,
                           "content_index": 0, "item_id": item["id"], "delta": answer}]
            response = {"id": "resp_" + request_id, "object": "response", "created_at": 1,
                        "model": CHAT_MODEL, "status": "completed", "output": [item]}
            if not request.get("stream"):
                self.write_body(json.dumps(response).encode())
                return
            events = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                *events,
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            stream = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            self.write_body((stream + "data: [DONE]\n\n").encode(), "text/event-stream")

    with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as gateway:
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{gateway.server_port}/v1", traffic
        finally:
            gateway.shutdown()
            worker.join()


def test_configure_magpie_image_model_and_render_result(tmp_path, monkeypatch, magpie_gateway):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    base, traffic = magpie_gateway
    settings = smoke._settings()
    settings["providers"]["custom"].update(
        protocol="openai-responses", name="Magpie fixture", base_url=base,
        api_key="magpie-ginno", model=CHAT_MODEL,
    )
    monkeypatch.setattr(smoke, "_settings", lambda: settings)
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port, fake_llm=False)
    try:
        smoke._wait_health(port)
        title = "Responses image fixture"
        smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path),
            "title": title, "provider": "custom", "model": CHAT_MODEL,
        })
        url = f"http://127.0.0.1:{port}"
        with sync_playwright() as playwright:
            browser, page = smoke._new_page(playwright)
            def select_chat():
                page.locator(".nav-item.group", has_text=title).click()

            def send(text, answer):
                composer = page.locator("textarea").first
                composer.fill(text)
                composer.press("Enter")
                expect(page.get_by_text(answer, exact=True).last).to_be_visible(timeout=15_000)

            page.goto(url + "/")
            select_chat()
            send("A regular text turn", "Chat is ready.")
            plain = next(
                body for path, body in traffic
                if path == "/v1/responses" and body.get("stream")
                and body.get("tools") and "A regular text turn" in json.dumps(body["input"])
            )
            assert all(tool.get("name") != "generate_image" for tool in plain.get("tools", []))
            assert all(path != "/v1/images/generations" for path, _ in traffic)

            page.goto(url + "/settings/connections/model-api")
            page.get_by_text("Magpie fixture", exact=True).click()
            switch = page.get_by_role("checkbox", name="Enable image generation")
            image_model = page.get_by_role("textbox", name="Image model ID")
            expect(switch).not_to_be_checked()
            expect(image_model).to_have_count(0)
            switch.focus()
            page.keyboard.press("Space")
            expect(switch).to_be_checked()
            expect(image_model).to_have_value("gpt-image-2")
            image_model.fill(IMAGE_MODEL)
            with page.expect_response(lambda reply: reply.url.endswith("/api/model_configs/verify")) as verified:
                page.get_by_role("button", name="Verify & Save", exact=True).click()
            saved = verified.value.json()["config"]
            assert saved["enable_image_generation"] is True
            assert saved["image_generation_model"] == IMAGE_MODEL
            assert saved["default_model"] == CHAT_MODEL
            assert all(path != "/v1/images/generations" for path, _ in traffic)

            page.goto(url + "/")
            select_chat()
            send(PROMPT, "Image is ready.")
            image = page.locator('img[src*="/api/files/"][src*="/download"]')
            expect(image).to_have_count(1)
            expect(image).to_be_visible()
            src = image.get_attribute("src")
            assert page.request.get(src).body() == PNG
            assert [body for path, body in traffic if path == "/v1/images/generations"] == [
                {"model": IMAGE_MODEL, "prompt": "A blue circle", "n": 1},
            ]
            image_turn = [
                body for path, body in traffic
                if path == "/v1/responses" and body.get("stream")
                and PROMPT in json.dumps(body["input"])
                and any(tool.get("name") == "generate_image" for tool in body.get("tools", []))
            ]
            first, followup = image_turn
            assert first["model"] == followup["model"] == CHAT_MODEL
            assert sum(tool.get("name") == "generate_image" for tool in first["tools"]) == 1
            assert any(item.get("type") == "function_call_output" for item in followup["input"])
            assert "ginno-images:" not in json.dumps(followup)

            page.reload()
            select_chat()
            expect(image).to_have_count(1)
            expect(image).to_have_attribute("src", src)
            assert sum(path == "/v1/images/generations" for path, _ in traffic) == 1
            browser.close()
    finally:
        smoke._stop(proc, log)
