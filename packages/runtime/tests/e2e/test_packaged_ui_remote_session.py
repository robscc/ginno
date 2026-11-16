"""Packaged-UI e2e: 新建对话选远端 A2A 节点作为执行方（remote-session-design.md R3）。

2026-11-12 起的规矩（CLAUDE.md 红线）：前端交互改动必须配 packaged UI 用例。
R3 前端已有源码断言 vitest（peerTarget/AgentChips/useComposerSend/
RemotePeerBadge/remoteSessionWiring），但「首页 peer chip → 建会话带
remote_peer → 会话头徽标」整条渲染层链路零覆盖，本文件补上。

与 test_packaged_ui_a2a.py 同基建：dist sidecar + 真实 Chromium，
GINNO_HOME 隔离到 tmp_path，bind(0) 随机空闲口（不碰 8787）。语言钉
zh-CN——断言词（「在 … 上运行」/「远端 ·」）取自 messages/zh-CN 的
chat.json / shell.json。

peer 不经接入向导，直接种 ``a2a/peers.json``（PeerStore 落盘形态，
PeerRecord.to_dict 同构）——R3 测的是**选择器与建会话链路**，向导链路
已由 test_packaged_ui_a2a.py 覆盖。对端 endpoint 指向 loopback:9
（discard，必然不可达）：远端 turn fail-fast 是设计定案 #7 的预期行为，
本文件只断言「会话归属 + UI 标识」，不断言远端 turn 成功。

覆盖四条链路：
1. 首页执行方选择器：peer chip 与本地 agent chip 并列，机器图标 + 地址
   提示；disabled peer 不出现。
2. GET /api/a2a/peers 脱敏：明文 token 绝不进响应。
3. 选 peer → 首发建会话带 remote_peer（API 真源）→ 会话头「远端 · name」
   徽标可见 → 本地 agent 切换 chips 整行隐藏（定案 #11）。
4. 不选 peer 的本地首发：会话无 remote_peer，徽标不出现（本地行为零回归）。

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
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST

PEER_SLUG = "e2e-remote"
PEER_NAME = "E2E 远端节点"
# loopback:9（discard）必然不可达 → 远端 turn fail-fast（定案 #7），
# 不影响「建会话归属 + UI 标识」断言。
PEER_ENDPOINT = "https://127.0.0.1:9/a2a"
PEER_TOKEN = "e2e-secret-token-abcdef123456"


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
    # language pin zh-CN：断言词依赖 locale 固定（与 a2a 用例同基线做法）。
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
                       "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _peer_record(slug: str, name: str, *, enabled: bool = True) -> dict:
    """PeerRecord.to_dict 同构的落盘形态（明文 token 只在磁盘）。"""
    return {
        "slug": slug,
        "id": f"a2a-{slug}",
        "name": name,
        "endpoint": PEER_ENDPOINT,
        "token": PEER_TOKEN,
        "peer_fingerprint": "",
        "auth_scheme": "none",
        "enabled": enabled,
        "trust": "readonly",
        "caps": ["L0"],
        "task_timeout_s": 600,
        "max_concurrent": 2,
        "confirm_mode": "every",
        "allow_local_files": False,
        "created_at": time.time(),
    }


def _api(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def _sessions(port: int) -> list:
    return _api(port, "/api/sessions?project_slug=default")


def _wait_session(port: int, pred, timeout: float = 20) -> dict:
    """轮询等满足条件的会话出现（落盘/列表刷新有竞态，不裸查一次）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _sessions(port):
            if pred(s):
                return s
        time.sleep(0.3)
    raise AssertionError("等待的会话未出现")


@pytest.fixture
def app(tmp_path):
    """Boot packaged sidecar（种好 peer 注册表）+ Chromium，yield (page, port)。"""
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
        json.dumps({
            PEER_SLUG: _peer_record(PEER_SLUG, PEER_NAME),
            "off-node": _peer_record("off-node", "停用节点", enabled=False),
        }),
        encoding="utf-8",
    )

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_RUNTIME_PORT=str(port),
    )
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


def _send_first_message(page, text: str) -> None:
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=30_000)
    ta.click()
    ta.fill(text)
    ta.press("Enter")


# ---- 1. 首页选择器：peer 与本地 agent 并列（定案 #10） -----------------------


def test_home_peer_chip_alongside_agents(app):
    page, port = app
    chip = page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}")
    chip.wait_for(state="visible", timeout=30_000)

    # 文案：zh-CN chat.remote-peer-chip-ask = 「在 {name} 上运行」
    assert PEER_NAME in chip.inner_text()
    assert "上运行" in chip.inner_text()
    # 地址提示：剥协议头与 #fp 后的 host:port/path 可见
    assert "127.0.0.1:9/a2a" in chip.inner_text()
    # 机器图标（lucide Server 渲染为 svg）
    assert chip.locator("svg").count() >= 1
    # tooltip 带完整 endpoint
    assert PEER_ENDPOINT in (chip.get_attribute("title") or "")

    # 与本地 agent chip 并列：同一 chip 行里既有「问 X」也有 peer chip
    agent_chips = page.locator("button").filter(has_text=re.compile(r"^问 "))
    assert agent_chips.count() >= 1, "本地 agent chips 应仍在（并列，不是替换）"

    # disabled peer 不出现
    assert page.get_by_test_id("remote-peer-chip-off-node").count() == 0

    # 后端读面与 UI 候选一致
    peers = _api(port, "/api/a2a/peers").get("peers") or []
    assert {p["slug"] for p in peers} == {PEER_SLUG, "off-node"}


# ---- 2. 脱敏契约：token 绝不进 GET 响应 --------------------------------------


def test_peers_api_never_leaks_token(app):
    _page, port = app
    raw = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/a2a/peers", timeout=10
    ).read().decode()
    assert PEER_TOKEN not in raw, "明文 token 泄漏进 /api/a2a/peers 响应"
    body = json.loads(raw)
    assert body.get("ok") is True
    peer = next(p for p in body["peers"] if p["slug"] == PEER_SLUG)
    # 选择器需要的展示/路由字段齐全，且无 token 键
    for field in ("slug", "id", "name", "endpoint", "enabled", "trust", "caps"):
        assert field in peer, field
    assert "token" not in peer


# ---- 3. 选 peer → 远端会话 + 会话头徽标 + 禁切换（定案 #10/#11） -------------


def test_select_peer_creates_remote_session_and_badge(app):
    page, port = app
    chip = page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}")
    chip.wait_for(state="visible", timeout=30_000)
    chip.click()
    # 选中态：violet 高亮类出现（未选中是 border-line 灰态）
    assert "violet" in (chip.get_attribute("class") or "")

    _send_first_message(page, "hello remote session")
    s = _wait_session(port, lambda x: x.get("remote_peer") == PEER_SLUG)
    assert s["remote_peer"] == PEER_SLUG

    # 会话头徽标：「远端 · E2E 远端节点」（shell.remote-badge zh-CN）
    badge = page.get_by_test_id("remote-peer-badge")
    badge.wait_for(state="visible", timeout=15_000)
    txt = badge.inner_text()
    assert "远端" in txt and PEER_NAME in txt
    assert PEER_ENDPOINT in (badge.get_attribute("title") or "")

    # 定案 #11：远端会话里本地 agent 切换 chips 整行隐藏，peer chip 也不在
    # （会话内不渲染执行方选择器——归属已定，不可中途切换）。
    deadline = time.time() + 10
    while time.time() < deadline:
        if page.locator("button").filter(has_text=re.compile(r"^问 ")).count() == 0:
            break
        page.wait_for_timeout(300)
    assert page.locator("button").filter(has_text=re.compile(r"^问 ")).count() == 0, (
        "远端会话不应再显示本地 agent 切换 chips（定案 #11）"
    )
    assert page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}").count() == 0

    # 远端 turn 对端不可达 → fail-fast（定案 #7），绝不静默回退本地：
    # 会话归属字段不被抹掉。
    time.sleep(2)
    s2 = next(x for x in _sessions(port) if x["id"] == s["id"])
    assert s2.get("remote_peer") == PEER_SLUG, "远端归属不得因 turn 失败被抹掉"


# ---- 4. 本地首发零回归：无 remote_peer、无徽标 --------------------------------


def test_local_send_has_no_remote_badge(app):
    page, port = app
    # 等 peer chip 渲染（证明选择器在线），但不选它
    page.get_by_test_id(f"remote-peer-chip-{PEER_SLUG}").wait_for(
        state="visible", timeout=30_000
    )
    _send_first_message(page, "hello local session")
    s = _wait_session(port, lambda x: True)
    assert not s.get("remote_peer"), "未选 peer 的会话必须是本地会话"
    assert page.get_by_test_id("remote-peer-badge").count() == 0
    # 本地会话的 agent 切换 chips 仍在（既有行为不回归）
    assert page.locator("button").filter(has_text=re.compile(r"^问 ")).count() >= 1