"""Packaged-UI e2e: LLM 请求级可观测行 + 远端会话模型 chip 禁用（T3 UI 侧）。

2026-11-12 起的规矩（CLAUDE.md 红线）：前端交互改动必须配 packaged UI 用例——
vitest/tsc 抓不到渲染层断裂。本文件覆盖两条链路的**浏览器级**终态：

A. llm.request / llm.response（runtime graph 埋点 → engine custom 翻译 →
   WS → handle.ts → applyBlock 按 (req_id, attempt) 合并 → LlmCallLine）：
   1. fake LLM 单调用 turn → 转录里**恰好一条** llm-call-line（request 与
      response 合并成一条的确定性判据：不合并会渲染成两条），终态带耗时与
      ↑/↓ token（placeholder 回复带 usage_metadata），无「请求中…」残留。
   2. 真实 provider 指向 loopback:1（必然 connection refused，不开 fake
      LLM）→ 失败尝试可见：✖ + 「失败 · <异常类型名>」，琥珀而非正红；
      非限流错误不重试 → 仍恰好一条。
B. 远端会话（remote_peer）模型 chip 禁用：
   3. 选 peer 建会话 → chip disabled + tooltip「模型由远端节点（name）决定」
      + label 显示 peer 名；程序化 click 发不出 PATCH、菜单不展开。
   4. 本地会话零回归：chip 可点、菜单正常展开（footnote 可见）。

与 test_packaged_ui_remote_session.py 同基建：dist sidecar + 真实 Chromium，
GINNO_HOME 隔离 tmp_path，bind(0) 随机空闲口（不碰 8787），语言钉 zh-CN
（断言词取自 messages/zh-CN/chat.json）。peer 直接种 a2a/peers.json，
endpoint 指向 loopback:9（discard）——远端 turn fail-fast 是定案 #7 预期，
本文件只断言 UI 控件状态，不断言远端 turn 成功。

Skips gracefully when the binary isn't built or playwright isn't installed.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST

PEER_SLUG = "e2e-llm-remote"
PEER_NAME = "E2E 远端节点"
PEER_ENDPOINT = "https://127.0.0.1:9/a2a"
PEER_TOKEN = "e2e-secret-token-abcdef123456"

# zh-CN 断言词（messages/zh-CN/chat.json）
PENDING_TEXT = "请求中…"          # llm-call-pending
FAILED_TEXT = "失败"              # llm-call-failed
REMOTE_TITLE_FRAG = "模型由远端节点"  # model-remote-title
LOCAL_TITLE_FRAG = "切换本会话的模型"  # model.switchTitle
MENU_FOOTNOTE_FRAG = "下一回合生效"   # model.footnote
# 审计 C2（2026-11-18）：composer 守卫 + 面板归属说明
REMOTE_ATTACH_FRAG = "远端会话暂不支持附件"  # composer.remoteAttachTitle
REMOTE_SLASH_FRAG = "远端会话暂不支持 / 命令"  # composer.remoteSlashTitle
MENU_ARIA_LABEL = "命令与提及补全"          # composer.menu.ariaLabel
CODE_TAB_LABEL = "代码"                     # right.tabs.code
REMOTE_FILES_FRAG = "对端节点"              # code.panel.remoteNote


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()


def _wait_health(port: int, timeout: float = 60) -> None:
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
    # language pin zh-CN；custom provider 指向 loopback:1（discard）——
    # fake LLM 模式下 base_url 无关紧要；失败用例（不开 fake）靠它必然
    # connection refused。
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "zh-CN",
        "providers": {
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "",
                          "default_model": "x", "base_url": "", "max_tokens": 1,
                          "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai", "api_key": "",
                       "default_model": "x", "base_url": "", "org_id": "",
                       "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t",
                       "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                       "default_model": "m", "max_tokens": 100, "temperature": 0.7,
                       "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _peer_record(slug: str, name: str) -> dict:
    """PeerRecord.to_dict 同构的落盘形态（与 remote_session 用例同）。"""
    return {
        "slug": slug,
        "id": f"a2a-{slug}",
        "name": name,
        "endpoint": PEER_ENDPOINT,
        "token": PEER_TOKEN,
        "peer_fingerprint": "",
        "auth_scheme": "none",
        "enabled": True,
        "trust": "readonly",
        "caps": ["L0"],
        "task_timeout_s": 600,
        "max_concurrent": 2,
        "confirm_mode": "every",
        "allow_local_files": False,
        "created_at": time.time(),
    }


def _sessions(port: int) -> list:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/sessions?project_slug=default", timeout=5
    ) as r:
        return json.loads(r.read().decode())


def _wait_session(port: int, pred, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _sessions(port):
            if pred(s):
                return s
        time.sleep(0.3)
    raise AssertionError("等待的会话未出现")


@contextmanager
def _boot(tmp_path, *, fake_llm: bool):
    """dist sidecar + Chromium，yield (page, port)。fake_llm=False 时走真实
    provider 路径（base_url=loopback:1 → 必然连接失败，供失败态用例）。"""
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")
    a2a_dir = home / "a2a"
    a2a_dir.mkdir()
    (a2a_dir / "peers.json").write_text(
        json.dumps({PEER_SLUG: _peer_record(PEER_SLUG, PEER_NAME)}),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_RUNTIME_PORT=str(port),
    )
    if fake_llm:
        env["GINNO_FAKE_LLM"] = "1"
    else:
        env.pop("GINNO_FAKE_LLM", None)
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            yield page, port
            browser.close()
    finally:
        sc.stop_sidecar(proc)


@pytest.fixture
def app(tmp_path):
    with _boot(tmp_path, fake_llm=True) as pair:
        yield pair


@pytest.fixture
def fail_app(tmp_path):
    with _boot(tmp_path, fake_llm=False) as pair:
        yield pair


def _send_first_message(page, text: str) -> None:
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=30_000)
    ta.click()
    ta.fill(text)
    ta.press("Enter")


def _wait_turn_settled(page, port, sid: str, timeout: float = 30) -> None:
    """轮询等 turn 收口（服务端 running 落定）。

    2026-11-17：llm_call 行按用户要求默认不渲染，故不能再拿「llm 行
    pending 消失」当收口判据——改为查服务端 running 态。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = _sessions(port)
            if rows and not any(x.get("running") for x in rows):
                return
        except Exception:
            pass
        page.wait_for_timeout(300)
    raise AssertionError("turn 未在超时内收口")


# ---- A1. 合并终态：恰好一条、带耗时与 token ----------------------------------


def test_llm_call_line_hidden_by_default(app):
    """llm_call 行**默认不渲染**（2026-11-17 用户反馈「不需要这个」）。

    数据链路保留：后端仍发 llm.request/llm.response（由
    tests/unit/test_llm_request_events.py 18 例钉住），reducer 仍合并成
    llm_call block，只是 blocks.tsx 不 push 渲染。本用例钉住「不可见」
    且**turn 本身不受影响**——隐藏不能把 turn 弄坏。
    """
    page, port = app
    _send_first_message(page, "hello llm observability")
    _wait_session(port, lambda x: True)
    _wait_turn_settled(page, port, "")
    # 给渲染留足时间，避免「还没渲染」被误判成「已隐藏」
    page.wait_for_timeout(1500)
    assert page.get_by_test_id("llm-call-line").count() == 0, (
        "llm_call 行应默认隐藏（用户要求），却渲染出来了"
    )
    # turn 正常收口：助手回复可见（隐藏 llm 行不得影响主消息渲染）
    body = page.inner_text("body")
    assert PENDING_TEXT not in body, "隐藏后不应有「请求中…」残留"


# ---- A2. 失败态可见：✖ + 失败 · 异常类型名（非限流不重试） --------------------


def test_llm_call_hidden_on_failure_path(fail_app):
    """失败路同样不渲染 llm 行，且 turn 级失败仍正常上报（不被隐藏吞掉）。"""
    page, port = fail_app
    _send_first_message(page, "hello failure path")
    _wait_session(port, lambda x: True)
    _wait_turn_settled(page, port, "", timeout=40)
    page.wait_for_timeout(1500)
    assert page.get_by_test_id("llm-call-line").count() == 0, (
        "失败路 llm_call 行也应隐藏"
    )
    assert PENDING_TEXT not in page.inner_text("body")


# ---- B1. 远端会话：模型 chip 禁用 + 归属说明 + 死控件不再发 PATCH -------------


def test_remote_session_model_chip_disabled(app):
    page, port = app
    chip = page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}")
    chip.wait_for(state="visible", timeout=30_000)
    chip.click()
    _send_first_message(page, "hello remote model guard")
    _wait_session(port, lambda x: x.get("remote_peer") == PEER_SLUG)

    # 会话头远端徽标先在线（归属事实可见）
    page.get_by_test_id("remote-peer-badge").wait_for(state="visible", timeout=15_000)

    # 模型 chip：远端态定位点 + disabled + tooltip 告知归属
    mchip = page.get_by_test_id("model-chip-remote")
    mchip.wait_for(state="visible", timeout=15_000)
    assert mchip.is_disabled(), "远端会话模型 chip 必须禁用（误导性死控件修复）"
    title = mchip.get_attribute("title") or ""
    assert REMOTE_TITLE_FRAG in title, f"tooltip 应告知模型由远端节点决定: {title!r}"
    assert PEER_NAME in title, f"tooltip 应带 peer 名: {title!r}"
    # label 显示 peer 名（不是本地 session.model——那与远端实际模型无关）
    assert PEER_NAME in mchip.inner_text(), f"chip label 应显示 peer 名: {mchip.inner_text()!r}"

    # 程序化 click（绕过 actionability）也发不出 PATCH、菜单不展开：
    # disabled 按钮 click-inert + useModelPick fail-closed 双保险。
    patch_bodies: list = []

    def _on_request(req):
        if req.method == "PATCH" and "/api/sessions/" in req.url:
            patch_bodies.append(getattr(req, "post_data", None))

    page.on("request", _on_request)
    page.evaluate(
        "() => document.querySelector('[data-testid=\"model-chip-remote\"]').click()"
    )
    page.wait_for_timeout(800)
    assert not patch_bodies, f"禁用 chip 不应发出模型 PATCH: {patch_bodies}"
    assert page.get_by_text(MENU_FOOTNOTE_FRAG, exact=False).count() == 0, (
        "禁用 chip 的菜单不应展开"
    )


# ---- B2. 本地会话零回归：chip 可点、菜单正常展开 ------------------------------


def test_local_session_model_chip_still_works(app):
    page, port = app
    # 等 peer chip 渲染（选择器在线）但不选它 → 本地会话
    page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}").wait_for(
        state="visible", timeout=30_000
    )
    _send_first_message(page, "hello local model chip")
    _wait_session(port, lambda x: True)

    # 本地 chip：无远端定位点，title 是常规切换文案，可点击
    assert page.get_by_test_id("model-chip-remote").count() == 0
    mchip = page.locator(f'button[title*="{LOCAL_TITLE_FRAG}"]').first
    mchip.wait_for(state="visible", timeout=15_000)
    assert mchip.is_enabled(), "本地会话模型 chip 必须仍可点击（零回归）"
    mchip.click()
    # 菜单展开：footnote 可见（zh-CN model.footnote）
    page.get_by_text(MENU_FOOTNOTE_FRAG, exact=False).first.wait_for(
        state="visible", timeout=10_000
    )


# ---- B3. 远端会话 composer 守卫：附件/slash 禁用 + 菜单 fail-closed -----------
# （2026-11-18 远程特性审计 C2：remote turn 只转发 user_text，附件/@mention/
# slash skill 到不了对端——入口禁用带原因；面板显示归属说明而非空列表。）


def _open_remote_session(page, port, text: str = "hello remote composer guard") -> None:
    chip = page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}")
    chip.wait_for(state="visible", timeout=30_000)
    chip.click()
    _send_first_message(page, text)
    _wait_session(port, lambda x: x.get("remote_peer") == PEER_SLUG)


def test_remote_session_composer_entrypoints_disabled(app):
    page, port = app
    _open_remote_session(page, port)
    page.get_by_test_id("remote-peer-badge").wait_for(state="visible", timeout=15_000)

    # 附件按钮：禁用 + 原因 tooltip（远端会话暂不支持附件）
    attach = page.get_by_test_id("composer-attach-remote")
    attach.wait_for(state="visible", timeout=15_000)
    assert attach.is_disabled(), "远端会话附件按钮必须禁用"
    title = attach.get_attribute("title") or ""
    assert REMOTE_ATTACH_FRAG in title, f"附件 tooltip 应说明远端不支持: {title!r}"

    # slash 按钮：禁用 + 原因 tooltip
    slash = page.get_by_test_id("composer-slash-remote")
    slash.wait_for(state="visible", timeout=15_000)
    assert slash.is_disabled(), "远端会话 slash 按钮必须禁用"
    title = slash.get_attribute("title") or ""
    assert REMOTE_SLASH_FRAG in title, f"slash tooltip 应说明远端不支持: {title!r}"

    # 纯文本通道不受影响：textarea 可编辑
    ta = page.locator("textarea").first
    assert ta.is_editable(), "远端会话 textarea 必须保持可编辑（纯文本发送通道）"

    # 手打 "/" 也不弹补全菜单（recomputeMenu fail-closed）
    ta.fill("/")
    page.wait_for_timeout(600)
    assert page.get_by_role("listbox", name=MENU_ARIA_LABEL).count() == 0, (
        "远端会话不应弹出 slash/@ 补全菜单"
    )
    ta.fill("@")
    page.wait_for_timeout(600)
    assert page.get_by_role("listbox", name=MENU_ARIA_LABEL).count() == 0, (
        "远端会话打 @ 不应弹 mention 菜单"
    )

    # plans 面板：显示对端管理说明卡（而非静默空白）
    note = page.get_by_test_id("plan-board-remote-note")
    note.wait_for(state="visible", timeout=15_000)
    assert PEER_NAME in note.inner_text(), (
        f"plans 说明应带 peer 名: {note.inner_text()!r}"
    )


def test_remote_session_code_panel_shows_note(app):
    page, port = app
    _open_remote_session(page, port, "hello remote code panel note")
    page.get_by_test_id("remote-peer-badge").wait_for(state="visible", timeout=15_000)

    # 右栏切到「代码」tab（默认 tab 顺序里可见；面板保持可打开）
    page.get_by_role("tab", name=CODE_TAB_LABEL, exact=False).first.click()
    note = page.get_by_test_id("code-panel-remote-note")
    note.wait_for(state="visible", timeout=15_000)
    body = note.inner_text()
    assert REMOTE_FILES_FRAG in body, f"文件面板说明应含「对端节点」语义: {body!r}"
    assert PEER_NAME in body, f"文件面板说明应带 peer 名: {body!r}"
    # 恒空的「会话工作区」根不再出现在根切换器（不渲染空列表；本地挂载保留）
    page.wait_for_timeout(400)
    assert page.get_by_text("Session workspace", exact=False).count() == 0, (
        "远端会话不应显示恒空的会话工作区根"
    )


# ---- B4. 本地会话零回归：附件/slash 可用、菜单正常弹出 ------------------------


def test_local_session_composer_entrypoints_still_work(app):
    page, port = app
    page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}").wait_for(
        state="visible", timeout=30_000
    )
    _send_first_message(page, "hello local composer guard")
    _wait_session(port, lambda x: True)

    # 无远端定位点；附件/slash 按钮可点
    assert page.get_by_test_id("composer-attach-remote").count() == 0
    assert page.get_by_test_id("composer-slash-remote").count() == 0
    ta = page.locator("textarea").first
    ta.click()
    ta.fill("/")
    page.get_by_role("listbox", name=MENU_ARIA_LABEL).first.wait_for(
        state="visible", timeout=10_000
    )
    # plans 面板：本地会话无说明卡（不渲染任何 remote 空态）
    page.wait_for_timeout(600)
    assert page.get_by_test_id("plan-board-remote-note").count() == 0
