"""Packaged-UI e2e: TODO 勾选触发大礼花（候选⑦，2026-11-21）。

判据（确定性，不依赖视觉回弹）：
1. 勾选未完成 TODO 后，body 下出现 fixed 全屏 canvas（礼花引擎懒建）；
2. 引擎粒子计数 > 0（window.__fwParts 只读钩子，渲染循环节拍同步）；
3. 取消勾选不触发（仅 undone→done 放礼花）。

按 CLAUDE.md 红线：前端交互改动配 packaged UI 用例，ui-e2e job 执行。
"""

import json as _json
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


def _api(port: int, method: str, path: str, body: dict | None = None):
    data = _json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json.loads(r.read().decode()) if r.status != 204 else {}


def _create_session_with_todo(port: int, title: str, done: bool = False) -> str:
    sid = _api(port, "POST", "/api/sessions", {"title": "fw-e2e"})["id"]
    t = _api(port, "POST", f"/api/sessions/{sid}/todos", {"title": title, "priority": "p2"})
    if done:
        _api(port, "PATCH", f"/api/sessions/{sid}/todos/{t['id']}", {"done": True})
    return sid


def test_packaged_ui_todo_check_fires_firework(tmp_path):
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
    sid = None
    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        sid = _create_session_with_todo(PORT, "firework check", done=False)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{PORT}/?session={sid}", wait_until="load")
                page.wait_for_timeout(1500)
                # 右侧面板切到 todo tab（aria-label 含 todo/待办）
                tab = page.locator('[role="tab"]')
                page.wait_for_timeout(500)
                target = None
                for i in range(tab.count()):
                    label = (tab.nth(i).get_attribute("aria-label") or "").lower()
                    if "todo" in label or "待办" in label:
                        target = tab.nth(i)
                        break
                assert target is not None, "未找到 todo tab"
                target.click()
                page.wait_for_timeout(600)

                row = page.locator(".group").first
                row.wait_for(state="visible", timeout=8000)
                row.locator("button").first.click()  # 勾选 → 礼花

                canvas = page.locator("body > canvas").first
                canvas.wait_for(state="attached", timeout=3000)
                count = page.evaluate("() => window.__fwParts ?? -1")
                assert count > 0, f"礼花粒子数应 > 0，实际 {count}"
            finally:
                browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_packaged_ui_todo_uncheck_no_firework(tmp_path):
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
        sid = _create_session_with_todo(PORT, "no firework", done=True)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{PORT}/?session={sid}", wait_until="load")
                page.wait_for_timeout(1500)
                tab = page.locator('[role="tab"]')
                target = None
                for i in range(tab.count()):
                    label = (tab.nth(i).get_attribute("aria-label") or "").lower()
                    if "todo" in label or "待办" in label:
                        target = tab.nth(i)
                        break
                target.click()
                page.wait_for_timeout(600)

                row = page.locator(".group").first
                row.wait_for(state="visible", timeout=8000)
                row.locator("button").first.click()  # 取消勾选 → 不放

                page.wait_for_timeout(400)
                count = page.evaluate("() => window.__fwParts ?? -1")
                assert count <= 0, f"取消勾选不应新增粒子，实际 {count}"
                assert page.locator("body > canvas").count() == 0, "未触发时不应建 canvas"
            finally:
                browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
