"""Open a local draw.io attachment with external browser requests blocked."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

# A 1 × 1 PNG embedded in a draw.io style value (draw.io omits `;base64`).
_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGJgAAgAAAD//wMABgAFV7+tQAAAAABJRU5ErkJggg=="
_DIAGRAM = f"""\
<mxGraphModel><root>
  <mxCell id="0"/><mxCell id="1" parent="0"/>
  <mxCell id="box" parent="1" vertex="1" value="Offline diagram"
          style="rounded=1;fillColor=#e1d5e7;strokeColor=#9673a6;">
    <mxGeometry x="20" y="20" width="160" height="80" as="geometry"/>
  </mxCell>
  <mxCell id="picture" parent="1" vertex="1"
          style="shape=image;image=data:image/png,{_PNG};">
    <mxGeometry x="220" y="20" width="60" height="60" as="geometry"/>
  </mxCell>
</root></mxGraphModel>
"""
_VIEWER = "/vendor/viewer-static.min.js"


def test_drawio_attachment_loads_offline(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    if smoke.RUNTIME_MODE == "packaged":
        script = smoke.RUNTIME_BIN.parent / "_internal" / "web_out" / _VIEWER.lstrip("/")
        assert script.with_suffix(".js.gz").is_file()
        assert not script.exists()

    fixture = tmp_path / "offline.drawio"
    fixture.write_text(_DIAGRAM, encoding="utf-8")
    port = smoke._free_port()
    base = f"http://127.0.0.1:{port}"
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        session = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default", "title": "Draw.io preview", "workspace": str(tmp_path),
        })
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                viewer_requests = []
                drawio_external_requests = []

                def local_only(route):
                    url = route.request.url
                    if url.startswith(f"{base}/"):
                        if urlsplit(url).path == _VIEWER:
                            viewer_requests.append(url)
                        route.continue_()
                    else:
                        if route.request.frame.url == "about:srcdoc":
                            drawio_external_requests.append(url)
                        route.abort()

                page.route("**/*", local_only)
                session_id = json.dumps(session["id"])
                page.add_init_script(f"localStorage.setItem('ginno-last-session', {session_id})")
                page.goto(base, wait_until="load")
                expect(page.get_by_text("Draw.io preview", exact=True).first).to_be_visible()
                with page.expect_response(lambda response: urlsplit(response.url).path == "/api/files") as upload:
                    page.locator('input[type="file"]').set_input_files(fixture)
                assert upload.value.json()["ok"]
                composer = page.locator("textarea").first
                composer.fill("Open the local diagram.")
                composer.press("Enter")
                chip = page.get_by_role("button", name=fixture.name, exact=True)
                expect(chip).to_be_visible()
                assert viewer_requests == []
                with page.expect_response(f"{base}{_VIEWER}") as viewer:
                    chip.click()
                assert viewer.value.ok
                expected_encoding = "gzip" if smoke.RUNTIME_MODE == "packaged" else None
                assert viewer.value.headers.get("content-encoding") == expected_encoding

                frame = page.frame_locator(f'iframe[title="{fixture.name}"]')
                expect(frame.get_by_text("Offline diagram", exact=True)).to_be_visible()
                expect(frame.locator('svg [fill="#e1d5e7"]')).to_be_visible()
                picture = frame.locator('svg image').filter(visible=True)
                image_url = f"data:image/png;base64,{_PNG}"
                expect(picture).to_have_attribute("xlink:href", image_url)
                assert picture.evaluate("""async (node) => {
                    const image = new Image();
                    image.src = node.href.baseVal;
                    await image.decode();
                    return image.naturalWidth;
                }""") == 1
                assert viewer_requests == [f"{base}{_VIEWER}"]
                assert drawio_external_requests == []
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)
