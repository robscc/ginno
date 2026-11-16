"""Packaged-UI e2e: running 会话侧栏「彩虹边缘条」（CLAUDE.md 红线：前端
交互改动必须配 packaged UI 用例）。

2026-11-17 用户选定方案 01：running 会话左边缘条由静态青色脉冲改为流动
彩虹竖条。用 GINNO_FAKE_LLM_DELAY 制造运行窗，断言侧栏 running 行渲染
.running-rainbow-edge（彩虹渐变 + ginno-rainbow-edge 动画）。
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
PORT = 8905


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
        "language": "zh-CN",
        "providers": {
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "", "default_model": "x", "base_url": "", "max_tokens": 1, "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai", "api_key": "", "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t", "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m", "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _api(port: int, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def test_packaged_ui_running_rainbow_edge(tmp_path):
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
    env = dict(
        os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_DELAY="6", GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        sess = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Rainbow Edge E2E",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(2000)
            page.get_by_text("Rainbow Edge E2E", exact=False).first.click()
            ta = page.locator("textarea").first
            ta.fill("跑起来")
            ta.press("Enter")
            # running 指示来自 /api/running 5s 轮询——留足窗口
            edge = page.locator(".running-rainbow-edge")
            edge.first.wait_for(timeout=15000)
            assert edge.count() >= 1
            bg = edge.first.evaluate("el => getComputedStyle(el).backgroundImage")
            assert "linear-gradient" in bg, bg
            anim = edge.first.evaluate("el => getComputedStyle(el).animationName")
            assert anim == "ginno-rainbow-edge", anim
            # 几何：3px 宽左边缘条
            box = edge.first.bounding_box()
            assert box and box["width"] <= 4, box
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()