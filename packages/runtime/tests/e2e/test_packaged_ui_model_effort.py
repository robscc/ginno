"""Packaged-UI e2e: 模型 chip 菜单的思考档位选择（一次点击即生效、不回弹）。

2026-11-14 用户反馈「在输入框调整思考难度要调整两次才能调整到位」。根因：
ModelChipMenu 难度档 onClick 旧实现分别调 pickModel + pickThinking，两条并发
PATCH 各自用完整 r.session 覆盖本地状态——模型 PATCH 的应答带着服务端处理它
那一刻的旧 thinking_effort，晚到就把乐观更新的新档位冲回旧值（读作「没生效」），
第二次点（服务端已存住新值）才到位。修复 = 合并成单条原子 PATCH
（pickModelAndThinking：provider+model+thinking_effort 一次下发）。

本用例与 test_packaged_ui_mention.py 同基建（dist sidecar + fake LLM + 真实
Chromium），断言修复后的确定性终态：
1. 打开模型 chip 菜单 → 行尾箭头内联展开思考档位（手风琴，#134）
2. 点一次 "High · deep" → chip 角标出现 locale 无关的 "HI"，且等待异步沉降后
   **不回弹**（旧竞态会在模型 PATCH 应答到达时把角标冲掉）
3. 服务端 session.thinking_effort 持久化为 "high"
4. 重开菜单 → "High · deep" 档仍呈选中态（violet 高亮）
"""

import os
import re
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
# 测试用模型名（足够独特，便于 chip / 菜单行定位）
MODEL = "effort-test-model"
# chip 角标缩写 locale 无关（ModelChipMenu.EFFORT_BADGE）：high→"HI"
# en locale 下难度档 label（messages/en/chat.json model.thinkingHigh）
HIGH_LABEL = "High · deep"
# en locale 下 chip button 的 title（model.switchTitle）前缀，用于稳锚 chip
CHIP_TITLE_FRAG = "Switch this session"
# 行尾展开箭头的 title（model.thinkingTitle）
ARROW_TITLE = "Thinking"


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
    # 与 test_packaged_ui_mention._settings 同基线（language pin en，难度档 label
    # 与 chip title 依赖 locale 固定）。custom provider 不声明 thinking_mode，
    # 故 effortDisabled=False（档位可选）。default_model 独特便于定位。
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "en",
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


def test_packaged_ui_model_effort_single_click_sticks(tmp_path):
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

            # 1) 造一个 session：首条消息（fake LLM 即回）。会话模型 = 默认
            #    provider(custom) 的 default_model = MODEL。
            ta = page.locator("textarea").first
            ta.click()
            ta.fill("hello effort test")
            ta.press("Enter")
            deadline = time.time() + 15
            while time.time() < deadline and len(_sessions(PORT)) == 0:
                page.wait_for_timeout(300)
            assert len(_sessions(PORT)) == 1, "first message should create one session"
            page.wait_for_timeout(500)

            # 2) 定位模型 chip（session 态 title=switchTitle）并点开菜单
            chip = page.locator(f'button[title*="{CHIP_TITLE_FRAG}"]').first
            assert chip.count() >= 1, "model chip button should be present in session composer"
            assert MODEL in chip.inner_text(), f"chip should show the session model, got {chip.inner_text()!r}"
            chip.click()
            page.wait_for_timeout(400)

            # 3) 行尾箭头（role=button, title=Thinking）内联展开思考档位手风琴
            arrow = page.locator(f'[role="button"][title="{ARROW_TITLE}"]').first
            assert arrow.count() >= 1, "model row should expose a thinking-effort expand arrow"
            arrow.click()
            page.wait_for_timeout(300)

            # 监听会话 PATCH 请求——这是区分新旧实现的**确定性判据**：
            # 修复后点一次难度档只发一条含 provider+model+thinking_effort 的原子
            # PATCH；旧实现发两条并发 PATCH（{provider,model} 与 {thinking_effort}），
            # 二者各自用完整 r.session 覆盖本地状态，模型 PATCH 晚到就把新档位冲回
            # （= 用户「点两次才到位」）。localhost 太快、角标回弹不一定复现，故以
            # PATCH 条数为准。
            patch_bodies: list = []

            def _on_request(req):
                if req.method == "PATCH" and "/api/sessions/" in req.url:
                    # playwright-python：post_data 是属性（Optional[str]），非方法。
                    body = getattr(req, "post_data", None)
                    try:
                        patch_bodies.append(json.loads(body) if body else {})
                    except Exception:
                        patch_bodies.append({})

            page.on("request", _on_request)

            # 4) 点一次 "High · deep" 难度档（= 选模型 + 设档 + 关闭，单条原子 PATCH）
            high = page.get_by_role("button", name=HIGH_LABEL, exact=True).first
            assert high.count() >= 1, f"effort option {HIGH_LABEL!r} should render after expand"
            high.click()

            # 5) 等异步沉降后断言。
            page.wait_for_timeout(1200)
            # 5a) 确定性判据：恰好一条 PATCH，三字段齐全（旧实现=两条 → 失败）。
            assert len(patch_bodies) == 1, (
                f"ONE effort click should issue exactly ONE atomic PATCH "
                f"(provider+model+thinking_effort); got {len(patch_bodies)}: {patch_bodies}"
            )
            assert patch_bodies[0].get("thinking_effort") == "high", (
                f"the single PATCH should carry thinking_effort=high, got {patch_bodies[0]!r}"
            )
            assert patch_bodies[0].get("model") == MODEL, (
                f"the single PATCH should carry model={MODEL}, got {patch_bodies[0]!r}"
            )
            # 5b) 角标不回弹（修复后确定性成立）。
            assert "HI" in chip.inner_text(), (
                f"after ONE click the chip badge should read HI and persist (no revert), "
                f"got {chip.inner_text()!r}"
            )

            # 6) 服务端持久化校验
            sess = _sessions(PORT)[0]
            assert sess.get("thinking_effort") == "high", (
                f"server should persist thinking_effort=high, got {sess.get('thinking_effort')!r}"
            )

            # 7) 重开菜单 → "High · deep" 档仍呈选中态（violet 高亮），证明状态
            #    未在客户端被冲掉。注意：手风琴 expanded 状态在菜单开合间保持
            #    （ModelChipMenu 组件不随 open 重挂载），故重开后档位可能已展开；
            #    仅在未见档位时才点箭头，避免把已展开的手风琴收起。
            chip.click()
            page.wait_for_timeout(400)
            high2 = page.get_by_role("button", name=HIGH_LABEL, exact=True)
            if high2.count() == 0:
                arrow2 = page.locator(f'[role="button"][title="{ARROW_TITLE}"]').first
                arrow2.click()
                page.wait_for_timeout(300)
            high2 = page.get_by_role("button", name=HIGH_LABEL, exact=True).first
            cls = high2.get_attribute("class") or ""
            assert re.search(r"violet", cls), (
                f"reopened menu should keep {HIGH_LABEL!r} highlighted as selected, class={cls!r}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
