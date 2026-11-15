"""Packaged-UI e2e: 空闲态性能治理（2026-11-15 性能审计 P0）。

真机 sample 实锤：空闲态主 WebContent 仍以 ~46 次/秒逐帧渲染，根因是
终态/停车态子代理的鸭子无限动画（收起节头也常驻）与右栏隐藏 pill 的
pulse 徽标。本用例在真实浏览器里验证治理后的渲染契约：

1. 两个子代理跑完（fake LLM 脚本）→ 侧栏 done 节头鸭**静态**：
   .gd-sway/.gd-badge/.gd-float/.gd-roll/.gd-hue 零挂载（旧版 done 鸭
   挂 sway+badge 无限动画，收起也渲染）；
2. 右栏折叠态隐藏 pill（opacity-0 常驻 DOM）内**无** pulse 徽标；
   hover 边缘条 toolbar 正常滑出（条件渲染不破坏交互）；
3. CSS 契约：.animate-pulse-finite 迭代 3 次后静态；.gd-sway 类已无
   无限动画定义（探针节点 computed style）。

范式同 test_packaged_ui_context_panel：独立端口 + 独立 GINNO_HOME +
GINNO_FAKE_LLM 脚本，不碰真运行时数据。
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


# 注意：fake 脚本按会话重放（每个子代理从 turn1 重新消费）→ spawn 会
# 嵌套繁殖，被 subagent 深度帽截断后全部终态。主会话直接子代理数不保证
# ≥2，但 SubagentBoard 单桶也渲染节头鸭，足够验证静态契约。
_SCRIPTS = [
    {
        "content": "",
        "tool_calls": [
            {"name": "spawn_subagent", "args": {"goal": "Say alpha and stop.", "acceptance": "alpha reported"}},
            {"name": "spawn_subagent", "args": {"goal": "Say beta and stop.", "acceptance": "beta reported"}},
        ],
    },
    {"content": "alpha done."},
    {"content": "beta done."},
    {"content": "Main turn done."},
]


def test_packaged_ui_idle_static_duck_and_dock(tmp_path):
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
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps(_SCRIPTS))

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts_file),
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)

            ta = page.locator("textarea").first
            ta.click()
            ta.fill("spawn two workers for idle-perf e2e")
            ta.press("Enter")

            # 等 running 鸭出现再等其消失（子代理终态；轮询等待，CI 慢机放大竞态）
            page.wait_for_selector(".ginno-duck", timeout=30000)
            page.wait_for_function(
                "document.querySelectorAll('.gd-roll, .gd-hue').length === 0",
                timeout=30000,
            )
            # 终态后 SubagentBoard 默认折叠（anyActive=false）——展开才渲染节头鸭。
            # 等折叠态稳定再点（点早了会把「运行中自动展开」反折成收起）；
            # 若仍无鸭（时序竞态）再点一次兜底。
            page.wait_for_timeout(1200)
            board_btn = page.locator("button").filter(has_text="🧭").first
            board_btn.click()
            page.wait_for_timeout(500)
            if page.locator(".ginno-duck").count() == 0:
                board_btn.click()
                page.wait_for_timeout(500)

            # 1) 终态鸭静态：无限动画类零挂载（旧版 done 挂 gd-sway+gd-badge）
            page.wait_for_function(
                "document.querySelectorAll('.gd-roll, .gd-hue').length === 0",
                timeout=30000,
            )
            animated = page.locator(".gd-sway, .gd-badge, .gd-float, .gd-roll, .gd-hue")
            assert animated.count() == 0, (
                f"terminal ducks must be static, found {animated.count()} animated nodes"
            )
            assert page.locator(".ginno-duck").count() >= 1, "done bucket header duck should render"

            # 2) 折叠态隐藏 pill 无 pulse 徽标；hover 滑出交互不破坏。
            # 默认右栏可能展开（RightPanel）——先收起才渲染 RightDock。
            if page.locator('[role="toolbar"]').count() == 0:
                page.locator('button[aria-label="Collapse panel"]').first.click()
                page.wait_for_timeout(400)
            toolbar = page.locator('[role="toolbar"]')
            assert toolbar.count() == 1, "right dock toolbar should exist (collapsed)"
            assert toolbar.locator(".animate-pulse, .animate-pulse-finite").count() == 0, (
                "hidden dock must not carry pulsing badges while idle"
            )
            page.mouse.move(1277, 400)  # 右边缘条 hover
            page.wait_for_timeout(400)
            assert "opacity-100" in (toolbar.get_attribute("class") or ""), (
                "hover should slide the dock out"
            )
            page.mouse.move(640, 400)
            page.wait_for_timeout(400)
            assert "opacity-0" in (toolbar.get_attribute("class") or ""), (
                "mouse leave should hide the dock"
            )

            # 3) CSS 契约探针：finite pulse 迭代 3 次；gd-sway 不再定义动画
            css = page.evaluate(
                """() => {
                    const mk = (cls) => {
                        const el = document.createElement('span');
                        el.className = cls;
                        document.body.appendChild(el);
                        const cs = getComputedStyle(el);
                        const out = { name: cs.animationName, iter: cs.animationIterationCount };
                        el.remove();
                        return out;
                    };
                    return { finite: mk('animate-pulse-finite'), sway: mk('gd-sway') };
                }"""
            )
            assert css["finite"]["iter"] == "3", f"finite pulse must iterate 3 times: {css}"
            assert css["finite"]["name"] != "none", "finite pulse keyframes must exist"
            assert css["sway"]["name"] == "none", (
                f"gd-sway infinite animation must be gone from CSS: {css}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)