"""Playwright e2e: worker 代答信封折卡片（worker ask_user 中断透传前端层,
2026-11-21）against the PACKAGED sidecar.

Same harness as test_packaged_ui_ask_user.py (real PyInstaller binary, temp
home, GINNO_FAKE_LLM, real Chromium). The subscriber-side render path is what
this pins: a human message carrying the ``<ginno_worker_question>`` envelope
(exactly what relay._inject_result injects into a subscriber session) must
fold into a card — worker title, question, original option chips — instead of
the raw XML text wall, and the card must SURVIVE A PAGE RELOAD (history path).

The envelope is sent AS the user message text so it lands in the checkpoint
verbatim (the real injection channel writes the same bytes); the fake model
replies once to finish the turn.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8895

WORKER_TITLE = "数据整理"
QID = "q_ui_e2e_001"
WORKER_SID = "w-ui-e2e-01"
QUESTION = "装到哪里？"
OPTIONS = ["Ginno 全局", "装到仓库"]
CARD_TITLE_ZH = "代答请求"

ENVELOPE = (
    f'<ginno_worker_question worker="{WORKER_SID}" qid="{QID}">\n'
    f'[waiting] Worker「{WORKER_TITLE}」停在一个需要用户决策的问题上，请你代为转达：\n'
    f"问题：{QUESTION}\n"
    "选项：\n"
    f"- {OPTIONS[0]}\n"
    f"- {OPTIONS[1]}\n"
    "\n"
    "转达方式：\n"
    "1. 用 ask_user 工具问你的用户。可以改述问题措辞，但每个选项的含义与数量必须与上面一致。\n"
    "2. 待转达问题 ≤3 个时，建议一次 ask_user(questions=[…]) 一屏问完。\n"
    "3. 用户回答后，把答案映射回上面的原选项，先写下映射理由，再调用 relay_answer。\n"
    f'4. relay_answer(worker_session="{WORKER_SID}", qid="{QID}", '
    'choice="<上面原选项之一的完整文本>", mapping_reason="…")\n'
    "</ginno_worker_question>"
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


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        import sys

        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()


def _wait_health(port: int, timeout: float = 60) -> None:
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
        # 卡片文案锚定中文（i18n 走 settings.language；auto 会按 headless
        # Chromium 的 en-US 解析成英文——首轮 e2e 红就是断言锚了中文标题
        # 而卡片渲染的是 "Relay needed"）。
        "language": "zh-CN",
        "providers": {
            "anthropic": {
                "enabled": False, "protocol": "anthropic", "api_key": "",
                "default_model": "x", "base_url": "", "max_tokens": 1,
                "temperature": 0.7, "timeout_s": 5,
            },
            "openai": {
                "enabled": False, "protocol": "openai", "api_key": "",
                "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1,
            },
            "custom": {
                "enabled": True, "protocol": "openai-compatible", "name": "t",
                "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                "max_tokens": 100, "temperature": 0.7, "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _scripts(path: Path) -> None:
    """One scripted turn: a plain reply (the envelope is the user message)."""
    path.write_text(
        json.dumps([{"content": "收到，我来转达这个问题。"}]),
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    _scripts(scripts)

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)
            yield page
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def _send_envelope(page) -> None:
    ta = page.locator("textarea").first
    ta.click()
    ta.fill(ENVELOPE)
    ta.press("Enter")
    # fake turn 收尾 + history 落盘
    page.wait_for_timeout(2500)


def _assert_card(page) -> None:
    card = page.locator(f"text={CARD_TITLE_ZH}").first
    card.wait_for(state="visible", timeout=8000)
    # worker 标题 chip
    assert page.locator(f"text={WORKER_TITLE}").first.is_visible()
    # 问题文本
    assert page.locator(f"text={QUESTION}").first.is_visible()
    # 原选项 chips 可见
    for opt in OPTIONS:
        assert page.locator(f"text={opt}").first.is_visible()
    # 信封 XML 不得再裸露（折卡片后 raw 只在「展开原文」里）
    body_text = page.locator("body").inner_text()
    assert "转达方式" not in body_text, "envelope instructions leaked as raw text"
    assert "mapping_reason" not in body_text, "envelope choice hint leaked as raw text"


def test_envelope_folds_into_card_and_survives_reload(app):
    page = app
    _send_envelope(page)
    _assert_card(page)
    # reload → history path renders the card from the checkpoint
    page.reload(wait_until="load")
    page.wait_for_timeout(1500)
    _assert_card(page)


def test_raw_envelope_expandable(app):
    page = app
    _send_envelope(page)
    btn = page.get_by_role("button", name="信封原文").first
    btn.wait_for(state="visible", timeout=8000)
    btn.click()
    page.wait_for_timeout(300)
    body_text = page.locator("body").inner_text()
    assert ENVELOPE.splitlines()[0] in body_text, "raw envelope not expandable"
    # 收起后 raw 再度隐藏
    page.get_by_role("button", name="收起").first.click()
    page.wait_for_timeout(300)
    assert "转达方式" not in page.locator("body").inner_text()
