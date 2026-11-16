"""Packaged-UI e2e: 字体族 + 字号设置（CLAUDE.md 红线：前端交互改动必须配
packaged UI 用例——vitest 守 fontPrefs 纯函数，这里守设置页交互 + 实际应用）。

覆盖（2026-11-16）：
1. 字号三档（小/中/大）点击 → 根元素 font-size 即时变化（14/16/18px）
2. 偏好持久化到 localStorage（ginno-font-prefs）
3. 字体族切换 → --app-font-family CSS 变量更新
4. 预览区可见且随字体族变化
"""

import os
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
PORT = 8903


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


def test_packaged_ui_font_settings(tmp_path):
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
            page.goto(f"http://127.0.0.1:{PORT}/settings/general", wait_until="load")
            page.locator('[data-testid="font-size-large"]').wait_for(timeout=15000)

            # ── 1. 字号三档 → 根 font-size 即时变化 ──
            page.locator('[data-testid="font-size-large"]').click()
            page.wait_for_timeout(200)
            assert page.evaluate("document.documentElement.style.fontSize") == "18px"
            assert page.locator('[data-testid="font-size-large"]').get_attribute("aria-pressed") == "true"

            page.locator('[data-testid="font-size-small"]').click()
            page.wait_for_timeout(200)
            assert page.evaluate("document.documentElement.style.fontSize") == "14px"

            page.locator('[data-testid="font-size-medium"]').click()
            page.wait_for_timeout(200)
            assert page.evaluate("document.documentElement.style.fontSize") == "16px"

            # ── 2. 持久化 localStorage ──
            page.locator('[data-testid="font-size-large"]').click()
            page.wait_for_timeout(200)
            persisted = page.evaluate("localStorage.getItem('ginno-font-prefs')")
            assert persisted and json.loads(persisted)["size"] == "large"

            # ── 3. 字体族 → CSS 变量 ──
            page.locator('[data-testid="font-family-select"]').select_option("serif")
            page.wait_for_timeout(200)
            fam = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--app-font-family')"
            )
            assert "ui-serif" in fam, fam
            persisted2 = page.evaluate("localStorage.getItem('ginno-font-prefs')")
            assert json.loads(persisted2)["family"] == "serif"

            # ── 4. 预览区可见 ──
            preview = page.locator('[data-testid="font-preview"]')
            assert preview.is_visible()
            assert "fox" in preview.inner_text().lower()

            # ── 5. 重载后偏好仍在（首帧应用）──
            page.reload(wait_until="load")
            page.locator('[data-testid="font-size-large"]').wait_for(timeout=15000)
            page.wait_for_timeout(300)
            assert page.evaluate("document.documentElement.style.fontSize") == "18px"

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()