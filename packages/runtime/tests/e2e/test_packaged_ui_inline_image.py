"""Playwright e2e: AIMessage 正文裸 markdown 图引用渲染（2026-11-21 811b… 报障
修复）against the PACKAGED sidecar.

Scenario that broke: an agent writes ``![帧②](v3-l-f2.png)`` into its answer,
referring to a screenshot it just generated into the session's artifacts dir.
The frontend resolved the bare relative name against the page URL → 404 → four
broken placeholder cards ("按钮状乱码块").

Fix under test (two layers):
1. backend ``messages_ui._resolve_inline_md_images`` rewrites such references
   into ``{kind:"image", fileId}`` blocks when the file exists in
   ``session_files_dir`` (history / post-turn rebuild path);
2. frontend ``Markdown.tsx`` renders a chip instead of a broken ``<img>`` for
   bare relative names that are NOT resolvable (live half-streamed frames).

Flow: send a plain turn (creates the session) → drop ``v3-l-f2.png`` into the
session artifacts dir → send ``![帧②](v3-l-f2.png)`` as the user message (the
fake model echoes it back so the *assistant* bubble also carries the reference
and goes through ``_messages_to_ui``) → reload → assert a real ``<img>`` with a
resolvable src and no broken-image card.
"""

from __future__ import annotations

import socket
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8892
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)  # 1x1 valid PNG


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "zh-CN",
        "providers": {
            "custom": {
                "enabled": True, "protocol": "openai-compatible", "name": "t",
                "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                "max_tokens": 100, "temperature": 0.7, "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


@pytest.fixture
def app(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    import os

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    # Turn 1: plain reply (session bootstrap). Turn 2: echo the markdown image
    # reference so the assistant bubble carries it through messages_ui.
    scripts.write_text(
        json.dumps(
            [
                {"content": "好的，会话已建立。"},
                {"content": "![白色系·帧②](v3-l-f2.png)"},
            ]
        ),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)
            yield page, home
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_inline_md_image_renders_as_img_after_reload(app):
    page, home = app
    # 1) bootstrap the session with a plain turn
    ta = page.locator("textarea").first
    ta.click(); ta.fill("开始"); ta.press("Enter")
    page.wait_for_timeout(2500)

    # 2) drop the artifact png into the session's artifacts dir
    sess_dir = next((home / "projects/default/sessions").glob("*/"))
    (sess_dir / "v3-l-f2.png").write_bytes(PNG)

    # 3) send the markdown-image turn (fake model echoes the reference)
    ta.click(); ta.fill("看这帧：\n![白色系·帧②](v3-l-f2.png)"); ta.press("Enter")
    page.wait_for_timeout(3000)

    # 4) reload → history path rebuilds blocks via _messages_to_ui
    page.reload(wait_until="load")
    page.wait_for_timeout(1800)

    imgs = page.locator("main img, div img")
    n = imgs.count()
    resolved = 0
    for i in range(n):
        src = imgs.nth(i).get_attribute("src") or ""
        if "/api/" in src or src.startswith(("data:", "blob:")):
            resolved += 1
    assert resolved >= 1, (
        f"expected at least one resolvable <img> (fileId URL), got {resolved}/{n}"
    )
    # no bare-name <img> left pointing at the page origin (404 shape)
    for i in range(n):
        src = imgs.nth(i).get_attribute("src") or ""
        assert not src.endswith("/v3-l-f2.png"), f"bare relative img leaked: {src}"
    body = page.locator("body").inner_text()
    # The reference may legitimately appear ONCE — the user's own message we
    # sent verbatim. The assistant's echoed copy must have been rewritten into
    # an image block, so a second occurrence means the rewrite leaked through.
    assert body.count("![白色系·帧②]") <= 1, "assistant copy of the reference was not rewritten"
