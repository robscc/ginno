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


def _wait_llm_lines(page, timeout: float = 20):
    """轮询等 llm-call-line 出现（流事件落 store + React 渲染有竞态）。"""
    loc = page.get_by_test_id("llm-call-line")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if loc.count():
            return loc
        page.wait_for_timeout(250)
    raise AssertionError("llm-call-line 未渲染（llm.request/response 链路断裂？）")


def _wait_turn_settled(page, port, sid: str, timeout: float = 30) -> None:
    """轮询等 turn 收口（服务端 running 落定 + 前端「请求中…」消失）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        lines = page.get_by_test_id("llm-call-line")
        n = lines.count()
        if n and all(PENDING_TEXT not in (lines.nth(i).inner_text() or "") for i in range(n)):
            return
        page.wait_for_timeout(300)
    raise AssertionError("turn 收口后仍有「请求中…」残留（response 未原地更新？）")


# ---- A1. 合并终态：恰好一条、带耗时与 token ----------------------------------


def test_llm_call_line_merged_single_row(app):
    page, port = app
    _send_first_message(page, "hello llm observability")
    _wait_session(port, lambda x: True)
    lines = _wait_llm_lines(page)
    _wait_turn_settled(page, port, "")

    # 确定性合并判据：fake placeholder 一次 turn 恰好一次模型调用 →
    # request/response 合并后**恰好一条**（不合并会渲染成两条）。
    assert lines.count() == 1, (
        f"一次模型调用应合并为一条 llm-call-line，got {lines.count()}（request/response 各渲染一条？）"
    )
    txt = lines.first.inner_text()
    # 终态无 pending 残留
    assert PENDING_TEXT not in txt, f"response 到达后仍显示「请求中…」: {txt!r}"
    # token 胶囊（placeholder 回复带 usage_metadata）：↑输入 / ↓输出
    assert "↑" in txt and "↓" in txt, f"终态应带 token 计量: {txt!r}"
    # 耗时胶囊（elapsed_ms → x.xs / Ns）
    assert re.search(r"\d+(\.\d+)?s", txt), f"终态应带耗时: {txt!r}"
    # fake 模型无 model_name → locale 无关回退 "LLM"（元数据退化不空白）
    assert "LLM" in txt, f"模型名缺失应回退 LLM: {txt!r}"
    # 紧凑行内条目：不是大卡片（无 button/展开控件，纯展示行）
    assert lines.first.locator("button").count() == 0


# ---- A2. 失败态可见：✖ + 失败 · 异常类型名（非限流不重试） --------------------


def test_llm_call_failure_visible(fail_app):
    page, port = fail_app
    _send_first_message(page, "hello failure path")
    _wait_session(port, lambda x: True)
    lines = _wait_llm_lines(page, timeout=40)
    _wait_turn_settled(page, port, "", timeout=40)

    # connection refused 非限流 → 不重试，恰好一条失败行
    assert lines.count() == 1, f"非限流失败不应重试出多行: {lines.count()}"
    txt = lines.first.inner_text()
    assert "✖" in txt, f"失败行应有 ✖ 标记: {txt!r}"
    assert FAILED_TEXT in txt, f"失败行应有「失败」文案: {txt!r}"
    # error 只带异常类型名（后端白名单契约）：失败 · <TypeName>
    assert re.search(r"失败 · [A-Za-z][A-Za-z0-9_]*", txt), (
        f"失败行应带异常类型名（不含消息正文）: {txt!r}"
    )
    assert PENDING_TEXT not in txt


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
