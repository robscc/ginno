"""Packaged-UI e2e: session 行右键菜单 + 手动「标为未读」（2026-11-16）。

链路验收：右键 session 行 → SessionContextMenu（动作与扇形菜单同源
useRowActions）→ 点「Mark as unread」→ POST /unread（forced_unread）→
侧栏行紫点（unread 装饰）出现。
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


def test_packaged_ui_session_context_menu_mark_unread(tmp_path):
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

            # 一轮 fake 对话 → 侧栏出现一条会话行
            ta = page.locator("textarea").first
            ta.click()
            ta.fill("unread check")
            ta.press("Enter")
            page.wait_for_timeout(1800)

            # 新建（回 home 落点）让会话行失去选中态——紫点渲染条件含 !sel
            page.get_by_title("Back to the landing", exact=False).first.click()
            page.wait_for_timeout(600)
            row = page.locator(".nav-item.group", has_text="unread check").first
            assert row.count() >= 1, "sidebar should show at least one session row"

            # 右键 → 上下文菜单出现，动作与扇形菜单同源（含 fork/pin/…）
            row.click(button="right")
            menu = page.locator('[role="menu"]')
            assert menu.count() >= 1, "context menu should open on right-click"
            assert page.locator('[role="menuitem"]', has_text="Mark as unread").count() == 1

            # 点「Mark as unread」→ 行内紫点（服务端 unread 装饰）出现
            page.locator('[role="menuitem"]', has_text="Mark as unread").click()
            page.wait_for_timeout(800)
            dot = page.locator('.nav-item.group [title*="unread" i]').first
            assert dot.count() >= 1, "unread dot should appear on the row"

            # 右键再次打开 → 现在是「Mark as read」（双态）
            row.click(button="right")
            assert page.locator('[role="menuitem"]', has_text="Mark as read").count() == 1
            page.locator('[role="menuitem"]', has_text="Mark as read").click()
            page.wait_for_timeout(800)
            assert page.locator('.nav-item.group [title*="unread" i]').count() == 0, (
                "dot should clear after mark-as-read"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_packaged_ui_opening_session_clears_manual_unread(tmp_path):
    """标为未读后**点开会话**应自动变已读（2026-11-16 事故回归）。

    事故：clearSessionUnread 只清 unreadRef map + 上报 /seen，不清 sessions
    数组里的 s.unread → 服务端 forced_unread 已清但本地乐观态残留，紫点与
    「标为已读」菜单项都留着。既有用例只测「菜单点标为已读」，漏了这条
    「打开即已读」路径（代码注释承诺的 IM 语义）。
    """
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

            ta = page.locator("textarea").first
            ta.click()
            ta.fill("open clears unread")
            ta.press("Enter")
            page.wait_for_timeout(1800)

            # 回 home 让行失去选中态（紫点条件含 !sel）
            page.get_by_title("Back to the landing", exact=False).first.click()
            page.wait_for_timeout(600)
            row = page.locator(".nav-item.group", has_text="open clears unread").first
            assert row.count() >= 1

            # 标为未读 → 紫点出现
            row.click(button="right")
            page.locator('[role="menuitem"]', has_text="Mark as unread").click()
            page.wait_for_timeout(800)
            assert page.locator('.nav-item.group [title*="unread" i]').count() >= 1, (
                "unread dot should appear after mark-as-unread"
            )

            # **点开会话**（不是走菜单）→ 应立即变已读
            row.click()
            page.wait_for_timeout(1000)
            # 切回 home 让行再次失去选中态，才能观察紫点是否真的清了
            page.get_by_title("Back to the landing", exact=False).first.click()
            page.wait_for_timeout(800)
            assert page.locator('.nav-item.group [title*="unread" i]').count() == 0, (
                "打开会话后紫点必须清除——s.unread 乐观态没清就会残留"
            )

            # 菜单也应回到「Mark as unread」双态（证明 unreadNow 已 false）
            row = page.locator(".nav-item.group", has_text="open clears unread").first
            row.click(button="right")
            assert page.locator('[role="menuitem"]', has_text="Mark as unread").count() == 1, (
                "打开会话后菜单应回到「标为未读」，说明已读态生效"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
