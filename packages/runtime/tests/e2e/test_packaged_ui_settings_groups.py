"""Packaged UI：设置页分组重组——MCP 工具 tab 移入智能体组（2026-11-19）。

用户需求：「mcp 工具 tab 移动到智能体菜单里面去放到 skill 后面」。
钉三件事：
1. 智能体组 tab 行里「技能」后面紧跟「MCP 工具」（顺序契约）；
2. 连接组 tab 行不再有「MCP 工具」；
3. 旧 URL /settings/connections/mcp 客户端重定向到 /settings/agents/mcp
   （静态导出的 LEGACY_SUB_REDIRECT 显式枚举，否则生产 404）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

RUNTIME_BIN = (
    Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime" / "ginno-runtime"
)
PORT = 18971


def _port_open(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.connect(("127.0.0.1", port))
            return True
    except OSError:
        return False


def _wait_health(port: int, timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            ) as r:
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


def test_packaged_ui_settings_mcp_tab_moved_to_agents(tmp_path):
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
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = subprocess.Popen(
        [str(RUNTIME_BIN)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()

            def tab_labels(path: str) -> list[str]:
                page.goto(f"http://127.0.0.1:{PORT}{path}", wait_until="load")
                page.wait_for_timeout(800)
                return page.eval_on_selector_all(
                    "main button, [role='tab'], nav button",
                    "els => els.map(e => (e.textContent||'').trim()).filter(Boolean)",
                )

            # 1) 智能体组：技能后面紧跟 MCP 工具
            labels = tab_labels("/settings/agents")
            assert "MCP 工具" in labels, f"智能体组缺 MCP 工具 tab：{labels[:12]}"
            assert "技能" in labels, f"智能体组缺技能 tab：{labels[:12]}"
            i_skills, i_mcp = labels.index("技能"), labels.index("MCP 工具")
            assert i_mcp == i_skills + 1, (
                f"MCP 工具应紧跟技能之后：skills@{i_skills} mcp@{i_mcp} {labels[:12]}"
            )

            # 2) 连接组：不再有 MCP 工具
            labels2 = tab_labels("/settings/connections")
            assert "MCP 工具" not in labels2, f"连接组仍有 MCP 工具 tab：{labels2[:12]}"

            # 3) 旧 URL 重定向到新家
            page.goto(
                f"http://127.0.0.1:{PORT}/settings/connections/mcp",
                wait_until="load",
            )
            page.wait_for_url("**/settings/agents/mcp", timeout=8000)
            assert page.url.endswith("/settings/agents/mcp"), page.url

            browser.close()
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
        except Exception:
            proc.kill()
