"""Packaged-UI e2e: 诊断面板 web_search「一键修复（安装 SearXNG）」（2026-11-15）。

背景：web_search 默认走 DuckDuckGo HTML 抓取，频繁被反爬（HTTP 202 验证码）/
连接重置，国内+代理环境尤甚。本特性在诊断面板加 web 搜索体检项：默认引擎为
DDG 时报 medium issue + 一键修复按钮；点击新建会话并按系统 prompt 触发 SearXNG
安装 turn，随后跳回聊天视图。

与 test_packaged_ui_model_effort.py 同基建（dist sidecar + fake LLM + 真实
Chromium）。断言确定性终态：
1. 诊断报告渲染 web 搜索 issue（DDG 脆弱默认）+ 「Fix (install SearXNG)」按钮
2. 点修复（接受 confirm）→ 新建一个会话 + 触发安装 turn（fake LLM 即回）
3. 视图跳回聊天首页 "/"

注意：fake LLM 不会真装 SearXNG，本用例只验证「按钮→建会话→触发 turn→跳转」
的接线，不验证安装结果（那是真实代理会话的职责）。
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
MODEL = "searxng-fix-test-model"
# en locale 文案（messages/en/settings.json diagnostics）
RUN_LABEL = "Run check"
FIX_LABEL = "Fix (install SearXNG)"
ISSUE_FRAG = "Fragile default search engine"


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
    # web 默认引擎 = duckduckgo（脆弱）→ 诊断应报 web issue + 修复按钮。
    # 其余与 model_effort 同基线：language pin en、custom provider、bypass。
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "en",
        "web": {"enabled": True, "default_engine": "duckduckgo", "max_results": 5, "timeout_s": 15, "engines": {}},
        "providers": {
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "", "default_model": "x", "base_url": "", "max_tokens": 1, "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai-compatible", "api_key": "", "base_url": "", "org_id": "", "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t", "api_key": "k", "base_url": "http://127.0.0.1:1", "model": MODEL, "default_model": MODEL, "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _sessions(port: int) -> list:
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/sessions?project_slug=default", timeout=5
    ) as r:
        return json.loads(r.read().decode())


def test_packaged_ui_web_search_searxng_fix(tmp_path):
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
            # confirm 弹窗一律接受（修复按钮点击前的 window.confirm）。
            page.on("dialog", lambda d: d.accept())
            page.goto(f"http://127.0.0.1:{PORT}/settings/diagnostics", wait_until="load")
            page.wait_for_timeout(2000)

            # 1) 跑体检 → 报告渲染
            run_btn = page.get_by_role("button", name=RUN_LABEL).first
            assert run_btn.count() >= 1, "diagnostics run button should be present"
            run_btn.click()

            # 2) 等 web issue + 修复按钮出现（轮询，避免裸查竞态）
            fix_btn = page.get_by_role("button", name=FIX_LABEL).first
            deadline = time.time() + 15
            while time.time() < deadline and fix_btn.count() == 0:
                page.wait_for_timeout(300)
            assert fix_btn.count() >= 1, "web-search fix button should render for DDG default"
            assert ISSUE_FRAG in page.content(), "web fragile-engine issue text should be shown"

            # 3) 点修复 → 建会话 + 触发安装 turn + 跳回聊天首页
            before = len(_sessions(PORT))
            fix_btn.click()
            deadline = time.time() + 15
            while time.time() < deadline and len(_sessions(PORT)) <= before:
                page.wait_for_timeout(300)
            assert len(_sessions(PORT)) > before, "fix should create a new install session"

            # 4) 视图跳回 "/"（router.push）
            deadline = time.time() + 10
            while time.time() < deadline and not page.url.rstrip("/").endswith(f":{PORT}"):
                page.wait_for_timeout(300)
            assert page.url.rstrip("/").endswith(f":{PORT}"), (
                f"fix should navigate back to the chat home '/', got {page.url!r}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
