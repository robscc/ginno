"""Packaged-UI e2e: TopBar 上下文圆环的预警口径（2026-11-16 P1）。

验证 threshold 数据端到端到达前端：usage 帧（engine.py）与 seed API
（api/usage.py）都经 usage.context_state 出货，圆环 tooltip 应出现
「auto-compacts at …」阈值文案，且环上有压缩触发刻度线。这是大窗口
模型预警倒挂修复（建议卡/圆环绑定 threshold 而非窗口百分比）的 UI 侧
验收。
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
PORT = 8899


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


def test_packaged_ui_context_ring_threshold_tooltip(tmp_path):
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

            # 一轮 fake 对话 → usage 帧带上 context（含新 threshold 字段）
            ta = page.locator("textarea").first
            ta.click()
            ta.fill("ring check")
            ta.press("Enter")
            page.wait_for_timeout(1500)

            # 圆环 span 的 title 含阈值文案（contextTitleThreshold，en）
            ring = page.locator('span[title*="auto-compacts at"]').first
            assert ring.count() >= 1, (
                "context ring tooltip should carry the auto-compaction threshold"
            )
            # 环上的压缩触发刻度线（threshold < window 时渲染 <line>）
            svg = ring.locator("svg")
            assert svg.count() >= 1
            assert svg.locator("line").count() >= 1, (
                "ring should draw the compaction-threshold tick"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
