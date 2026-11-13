"""Packaged-UI e2e: 上下文管理面板（2026-11-13 P0）。

覆盖用户报的“面板不合理”三条主症：
1. 打开后先给账本（per request + 分项），不再是孤零零的“省 0.0k”；
2. 工具小计来自真实测量（打包环境同时验证 tiktoken 词表随包可用，
   不允许静默降级成字符估算）；
3. 搜索框存在、Done 能关闭。

新 PR 的前端交互用例按 CLAUDE.md 要求纳入 `test_packaged_ui_*`，由
ci.yml 的 ui-e2e job 在 macOS runner 上真实执行。
"""

import os
import socket
import time
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8898
TITLE = "Context Manager"


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


def _wait_health(port: int, timeout: float = 40) -> None:
    import urllib.request

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
        "language": "en",
        "providers": {
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "", "default_model": "x", "base_url": "", "max_tokens": 1, "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai", "api_key": "", "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t", "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m", "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def test_packaged_ui_context_panel_ledger_and_search(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)

            # 首条消息创建会话（fake LLM 即回），随后出现 composer 工具条
            ta = page.locator("textarea").first
            ta.click()
            ta.fill("hello context panel")
            ta.press("Enter")
            page.wait_for_timeout(1200)

            trigger = page.locator(f'button[title="{TITLE}"]').first
            assert trigger.count() >= 1, "context manager entry should exist"
            trigger.click()
            page.wait_for_timeout(800)

            panel = page.locator("div.absolute.bottom-full").filter(has_text="per request").last
            assert panel.count() >= 1, "panel should lead with the per-request ledger"
            panel_text = panel.inner_text()
            print("PANEL TEXT:\n" + panel_text)
            assert "saves " not in panel_text, "panel must not show the old saves line"
            assert "MCP SERVERS" in panel_text, "MCP section should render"
            assert "tools ≈" in panel_text, "tools subtotal should be measured"
            assert "estimated" not in panel_text, (
                "tiktoken should load from the bundled vocab (no char-estimate fallback)"
            )
            assert page.locator('input[placeholder*="search servers"]').count() >= 1, (
                "search input should be present"
            )

            page.get_by_role("button", name="Done").first.click()
            page.wait_for_timeout(300)
            assert page.locator("text=per request").count() == 0, "Done should close the panel"
            browser.close()
    finally:
        sc.stop_sidecar(proc)