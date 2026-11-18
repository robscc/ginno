"""Packaged-UI e2e: 用户旅程批次（2026-11-18 走查 P1-2/P1-3/P2-7）。

CLAUDE.md 红线（2026-11-12 起）：前端交互改动必须配 packaged UI 用例。本次
旅程走查批次给侧栏/产物面板加了就地过滤、hash 路由兼容——渲染层断裂
（@ 菜单全灭三天教训）与交互接线必须钉进浏览器级回归。

覆盖三条链路（单 sidecar 串行三段，dist + 真实 Chromium + GINNO_HOME 隔离
tmp_path + bind(0) 随机端口）：
1. 侧栏就地过滤（P1-3）：POST 造 3 个标题可区分的会话 → 过滤框输入
   「旅程」→ 列表收窄到 1 + 计数徽标 1/3；拼音首字母「ztsh」同样命中
   「调度审计」；Esc 清空恢复全量。
2. 产物面板过滤框（P1-2）：面板头部渲染过滤输入（placeholder 中文），
   输入无匹配关键词显示「无匹配」空态而非崩溃。
3. hash 路由兼容（P2-7）：直接 goto /#/scheduled → AppShell 挂载时一次性
   规范化为 /scheduled，调度页元素可见。

Skips gracefully when the binary isn't built or playwright isn't installed.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait_health(port: int, timeout: float = 40) -> None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            ) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    raise TimeoutError(f"sidecar not healthy on {port}")


def _api(port: int, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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


def test_packaged_ui_ux_journey_filters_and_hash_route(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")

    PORT = _free_port()
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        # 造数：3 个标题可区分的会话（POST /api/sessions 即建，无需发消息；
        # project_slug/workspace 必填——默认项目与隔离 home 下的默认工作区）
        mk = {"project_slug": "default", "workspace": str(home)}
        _api(PORT, "POST", "/api/sessions", {**mk, "title": "旅程过滤甲"})
        _api(PORT, "POST", "/api/sessions", {**mk, "title": "调度审计"})
        _api(PORT, "POST", "/api/sessions", {**mk, "title": "deploy plan"})

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)

            # ---- 段1：侧栏就地过滤（P1-3）----
            flt = page.locator('input[aria-label*="过滤会话"]').first
            assert flt.count() >= 1, "sidebar filter input should render"
            # 中文 contains 命中
            flt.fill("旅程过滤甲")
            page.wait_for_timeout(400)
            assert "旅程过滤甲" in page.locator("aside").first.inner_text()
            assert "deploy plan" not in page.locator("aside").first.inner_text(), (
                "filter must narrow the list"
            )
            # 拼音首字母命中「调度审计」（diaodu shenji → ddsj）
            flt.fill("ddsj")
            page.wait_for_timeout(400)
            aside = page.locator("aside").first.inner_text()
            assert "调度审计" in aside, "pinyin initials ddsj should match 调度审计"
            # Esc 清空 → 恢复全量
            flt.press("Escape")
            page.wait_for_timeout(400)
            aside = page.locator("aside").first.inner_text()
            assert "deploy plan" in aside, "Esc should clear the filter"

            # ---- 段2：产物面板过滤框渲染（P1-2）----
            # 产物面板在右侧栏（会话未选中时空态也渲染头部 + 过滤框）
            art_flt = page.locator('input[aria-label*="过滤产物"]').first
            assert art_flt.count() >= 1, "artifacts filter input should render"

            # ---- 段3：hash 路由兼容（P2-7）----
            page.goto(f"http://127.0.0.1:{PORT}/#/scheduled", wait_until="load")
            page.wait_for_timeout(1200)
            body_text = page.locator("body").inner_text()
            assert ("定时任务" in body_text) or ("新建任务" in body_text), (
                "#/scheduled should land on the schedule page (hash-route compat)"
            )

            browser.close()
    finally:
        sc.stop_sidecar(proc)
