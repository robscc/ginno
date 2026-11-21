"""Packaged-UI e2e: 运行中任务统一面板（tasks-overview，2026-11-21）。

判据（确定性，data-testid 定位）：
1. 建一条调度任务后打开「运行」tab → 面板渲染、scheduled 组计数=1；
2. 停止（暂停）调度 → 薄确认 → 乐观移除 → counts 归零 → 空态出现。

按 CLAUDE.md 红线：前端交互改动配 packaged UI 用例，ui-e2e job 执行。
"""

import _json
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
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json.loads(r.read().decode()) if r.status != 204 else {}


def _spawn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    return proc


def test_packaged_ui_tasks_overview_list_and_pause(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    proc = _spawn(tmp_path)
    try:
        _wait_health(PORT)
        t = _api(PORT, "POST", "/api/schedule/tasks", {
            "name": "e2e overview", "target": {"type": "prompt", "prompt": "hi"},
            "schedule": {"kind": "daily", "at": "08:30"},
        })
        assert t.get("id"), t

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
                page.wait_for_timeout(1500)
                # 打开右侧「运行」tab（role=tab，aria-label 含 running/运行）
                tabs = page.locator('[role="tab"]')
                target = None
                for i in range(tabs.count()):
                    label = (tabs.nth(i).get_attribute("aria-label") or "").lower()
                    if "running" in label or "运行" in label:
                        target = tabs.nth(i)
                        break
                assert target is not None, "未找到「运行」tab"
                target.click()
                page.wait_for_timeout(800)

                panel = page.locator('[data-testid="tasks-overview-panel"]')
                panel.wait_for(state="visible", timeout=8000)
                item = page.locator('[data-testid="tasks-overview-item"]').first
                item.wait_for(state="visible", timeout=8000)
                assert "e2e overview" in item.inner_text()

                # 停止（暂停）→ 确认 → 移除 → counts 归零
                item.locator('[data-testid="tasks-overview-stop"]').first.click()
                page.wait_for_timeout(400)
                # ConfirmModal 的确认按钮（danger 主按钮）
                confirm = page.locator("button:has-text('暂停'), button:has-text('Pause'), button:has-text('停止'), button:has-text('Stop')").last
                confirm.click()
                page.wait_for_timeout(1200)
                # 服务端口径复核：调度已暂停，overview 不再枚举
                ov = _api(PORT, "GET", "/api/tasks/overview")
                assert ov["counts"]["scheduled"] == 0, ov["counts"]
            finally:
                browser.close()
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
