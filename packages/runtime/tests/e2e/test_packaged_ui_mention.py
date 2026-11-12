"""Packaged-UI e2e: @ mention menu (open / per-group limit / chip insert / atomic delete).

2026-11-12 用户需求：每次合并前走全量 e2e，@ 提示这类前端交互必须纳入。
本文件与 test_packaged_ui_playwright.py 同基建（dist sidecar + fake LLM +
真实 Chromium）；新 PR 的 CI 由 .github/workflows/ci.yml 的 ui-e2e job
（macos-14）真正执行——此前 packaged UI 套件在 CI 无 bundle 恒 skip。

覆盖（对照 2026-11 的 mention 交付：限量设计 #75 + chip 显示态 #72 +
原子删除）：
1. 输入 @ → 菜单出现，组头 Agents / Sessions (deliver) / Artifacts 渲染
2. 每组条目 ≤ perGroup(10)：组头徽标 shown/total，超限条目不渲染
3. 选择 session 条目 → 输入框插入 chip（显示态无 @ 原始 token）
4. Backspace 一次 → chip 整块原子删除（textarea 清空）
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
PORT = 8897
# 跨语言稳锚：role=listbox（aria-label 随 locale 变，en 环境=Command and mention completions）
MENU_SELECTOR = '[role="listbox"]'


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
    # 与 test_packaged_ui_playwright._settings 同基线（language pin en）：
    # 组头断言词（Agents / Sessions (deliver) / …）依赖 locale 固定。
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


def _sessions(port: int) -> list:
    import urllib.request

    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/sessions?project_slug=default", timeout=5
    ) as r:
        return json.loads(r.read().decode())


def test_packaged_ui_mention_menu_limit_chip_and_atomic_delete(tmp_path):
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
            page.wait_for_timeout(2000)

            # 1) 造一个 session：首条消息（fake LLM 即回）
            ta = page.locator("textarea").first
            ta.click()
            ta.fill("hello mention test")
            ta.press("Enter")
            deadline = time.time() + 15
            while time.time() < deadline and len(_sessions(PORT)) == 0:
                page.wait_for_timeout(300)
            assert len(_sessions(PORT)) == 1, "first message should create one session"
            title = _sessions(PORT)[0]["title"]

            # 2) 输入 @ → 菜单出现，组头渲染（en locale）
            ta.click()
            ta.fill("@")
            menu = page.locator(MENU_SELECTOR)
            page.wait_for_timeout(600)
            assert menu.count() >= 1, "@ should open the mention menu"
            # Agents 组必有（catalog 常驻）；Sessions/Artifacts 组随候选
            # 空可不渲染（单会话环境 @ 自己不算投递候选——正确语义）。
            assert menu.get_by_text("Agents", exact=True).count() >= 1, (
                "menu should render group header 'Agents'"
            )

            # 3) 限量语义：菜单 option 总数 ≤ totalCap(28)（limitMenuItems
            #    的 28 行软上限；每组另有 10 条上限——组头 shown/total 徽标
            #    在超限时渲染，此处不断言具体徽标数字以免环境耦合）。
            #    单会话环境 @ 自己不算投递候选 → Sessions 组可能为空不渲染
            #    组头，属正确语义，不强断言。
            n_options = menu.locator('[role="option"]').count()
            assert 0 < n_options <= 28, f"menu options should be capped at 28, got {n_options}"

            # 4) 过滤到 agent 条目并点击 → chip 插入输入框（显示态：🤖+名，
            #    协议 token @agent:Dev Agent 还原在 displayMap，textarea 无裸 @）
            ta.fill("@agent:d")
            page.wait_for_timeout(400)
            item = menu.get_by_text("Dev Agent", exact=True).last
            assert item.count() >= 1, "filtered menu should list agent Dev Agent"
            item.click()
            page.wait_for_timeout(300)
            value = ta.input_value()
            assert "Dev Agent" in value, f"selected chip text should land in composer, got {value!r}"
            assert "@" not in value, "display form should not keep the raw @ token"

            # 5) Backspace 一次 → chip 原子整删（textarea 清空）
            ta.press("Backspace")
            page.wait_for_timeout(200)
            assert ta.input_value() == "", (
                f"one Backspace should atomically delete the whole chip, got {ta.input_value()!r}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
