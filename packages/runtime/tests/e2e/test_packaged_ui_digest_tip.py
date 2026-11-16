"""Packaged-UI e2e: digest 文件传递 tip 渲染（CLAUDE.md 红线：前端交互改动
必须配 packaged UI 用例）。

通过 steer 注入一个 via_file 的 <ginno_worker_result kind="digest"> 信封
（live steer.absorbed 与历史重放共用 foldSubagentResultBlocks），断言折出的
员工回传卡显示「通过文件传递」tip + 文件位置。用 bash sleep 制造运行窗，
运行中 composer 发送即 steer。
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
PORT = 8904

ENVELOPE = (
    '<ginno_worker_result session="worker-x" from="测试员工" status="running" '
    'goal="进度" kind="digest" via_file="1" file="/tmp/ginno-digests/digest-x.md">'
    '[running] 这是一段完整的进度内容，通过文件传递，不应被截断。'
    '</ginno_worker_result>'
)


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


def test_packaged_ui_digest_via_file_tip(tmp_path):
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
    # 剧本：先调 bash sleep 3（制造运行窗供 steer），再收尾文本。
    scripts = tmp_path / "scripts.json"
    scripts.write_text(json.dumps([
        {"content": "", "tool_calls": [{"name": "bash", "args": {"command": "sleep 3"}}]},
        {"content": "收尾"},
    ]))

    env = dict(
        os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts), GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        sess = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Digest Tip E2E",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(2000)
            page.get_by_text("Digest Tip E2E", exact=False).first.click()
            ta = page.locator("textarea").first
            ta.fill("开始")
            ta.press("Enter")
            # 运行窗内（bash sleep 3）发送信封 → steer → 折卡
            page.wait_for_timeout(800)
            ta.fill(ENVELOPE)
            ta.press("Enter")
            tip = page.locator('[data-testid="digest-via-file-tip"]')
            tip.wait_for(timeout=12000)
            assert tip.is_visible()
            assert "/tmp/ginno-digests/digest-x.md" in tip.inner_text()
            assert "通过文件传递" in tip.inner_text()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()