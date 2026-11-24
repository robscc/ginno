"""Packaged-UI e2e: A2A 对端节点子配置（2026-11-18 连接器页改造）。

CLAUDE.md 红线（2026-11-12 起）：前端交互改动必须配 packaged UI 用例。本次
改造把 a2a-* peer 从顶层独立卡收进 A2A hub 详情的「对端节点」页签
（kind="a2a-peers"），面板可用性必须钉进回归——用户报过「设置抽屉点不开」
（父会话未复现），且 @ 菜单全灭三天才被发现的教训说明 vitest/tsc 抓不到
渲染层断裂。

与 test_packaged_ui_a2a.py 同基建：dist sidecar + 真实 Chromium，
GINNO_HOME 隔离 tmp_path，bind(0) 随机端口（不碰 8787 与兄弟文件固定段），
语言钉 zh-CN（断言词取自 messages/zh-CN/a2a.json）。peer 经 API
（a2a_add_peer）造数，端点指 127.0.0.1:9（discard，必然不可达）——重连
探针的失败路径因此确定性。

覆盖五条链路：
1. 顶层列表不再出现 a2a-* peer 卡（后端 hide_in_list 契约 + DOM 双断言），
   hub 卡保留、详情/配置 API 仍按 id 解析 peer；
2. hub「设置」面板可打开且渲染设置字段（用户报过点不开——回归钉死）；
3. 「对端节点」页签打开（自定义 kind 保留后端双语 title），行渲染
   名称/端点/信任级别/连接状态；
4. 行内子配置编辑器（内嵌 ConfigEditor，PATCH 到 peer 实例 id）可打开、
   修改信任级别并保存，落盘经 API 复核；
5. 重连探针失败路径（确定性：discard 端口不可达）+ 删除确认流（行消失、
   PeerStore 清空）。

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

# 假对端：loopback 9 端口（discard）必然不可达 → 重连探针必失败（确定性）。
PEER_NAME = "Sub Peer One"
PEER_SLUG = "sub-peer-one"
PEER_ENDPOINT = "https://127.0.0.1:9/a2a"


def _free_port() -> int:
    """bind(0) 让 OS 分配空闲端口（随机口，不碰 8787/兄弟文件固定段）。"""
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
    # language pin zh-CN：断言词依赖 locale 固定（与 a2a 兄弟文件同基线）。
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


def _api_get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def _api_post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _add_peer(port: int) -> dict:
    """经 hub action 造一个 peer（与接入向导同一条后端链路）。"""
    r = _api_post(port, "/api/connectors/a2a/action", {
        "action": "a2a_add_peer",
        "endpoint": PEER_ENDPOINT,
        "name": PEER_NAME,
    })
    assert r.get("ok") is True, r
    assert r.get("slug") == PEER_SLUG, r
    return r


@pytest.fixture
def app(tmp_path):
    """Boot packaged sidecar + Chromium，yield (page, port)。"""
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import expect, sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    port = _free_port()

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")

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
            page = browser.new_page(locale="zh-CN")
            # 首帧从语言镜像恢复，避免等待 settings 时先渲染英文或发出英文详情请求。
            page.add_init_script("localStorage.setItem('ginno.language', 'zh-CN')")
            page.goto(f"http://127.0.0.1:{port}/connectors", wait_until="load")
            expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
            yield page, port
            browser.close()
    finally:
        sc.stop_sidecar(proc)


# ---- 共用助手 ---------------------------------------------------------------


def _open_a2a(page):
    """从连接器列表点开 A2A hub 详情抽屉，返回 drawer locator。"""
    card = page.locator('[role="button"]').filter(has_text="A2A").first
    card.wait_for(state="visible", timeout=30_000)
    card.click()
    drawer = page.locator('[role="dialog"][aria-label*="A2A"]')
    drawer.wait_for(state="visible", timeout=15_000)
    # 等详情抽屉的入站面板渲染。
    drawer.get_by_text("入站 · 被其他 Agent 连接", exact=False).wait_for(timeout=15_000)
    return drawer


def _open_peers_tab(page, drawer):
    """打开「对端节点」页签并等清单渲染，返回 pane locator。"""
    tab = page.get_by_test_id("conn-tab-a2a-peers")
    tab.wait_for(state="visible", timeout=15_000)
    tab.click()
    pane = page.get_by_test_id("a2a-peers-pane")
    pane.wait_for(state="visible", timeout=15_000)
    return pane


def _open_peer_editor(page, pane):
    """展开行内子配置编辑器并等 ConfigEditor 字段渲染。"""
    pane.get_by_test_id(f"a2a-peer-config-{PEER_SLUG}").click()
    # 字段标签来自 a2a 域 i18n（fieldText：connectorId 以 a2a 开头）
    pane.get_by_text("能力地址", exact=False).first.wait_for(timeout=15_000)
    pane.get_by_text("信任级别", exact=False).first.wait_for(timeout=15_000)


# ---- 1. peer 不再占顶层卡位 --------------------------------------------------


def test_peer_hidden_from_top_level_list(app):
    page, port = app
    _add_peer(port)
    page.reload(wait_until="load")
    page.locator('[role="button"]').filter(has_text="A2A").first.wait_for(
        state="visible", timeout=30_000)

    # 后端契约：列表接口无 kind="a2a" 的 peer 条目；hub（kind=builtin）仍在
    payload = _api_get(port, "/api/connectors")
    entries = payload.get("connectors") or []
    ids = [c.get("id") for c in entries]
    assert "a2a" in ids  # hub 卡保留
    assert not any(str(i).startswith("a2a-") for i in ids)  # peer 不占顶层
    assert all(c.get("kind") != "a2a" for c in entries)

    # DOM：hub 卡存在，peer 名不作为顶层卡出现
    assert page.locator('[role="button"]').filter(has_text=PEER_NAME).count() == 0

    # 隐藏≠注销：详情/配置 API 仍按 id 解析（子配置编辑的取数路径）
    detail = _api_get(port, f"/api/connectors/a2a-{PEER_SLUG}")
    assert detail.get("id") == f"a2a-{PEER_SLUG}"
    assert (detail.get("config") or {}).get("endpoint") == PEER_ENDPOINT


# ---- 2. hub 设置面板可打开且渲染设置字段（用户报过「点不开」→ 回归钉死）------


def test_hub_settings_panel_opens_and_renders_fields(app):
    page, port = app
    drawer = _open_a2a(page)
    # 「设置」页签（action strip 的快捷按钮与页签同名，用 tab 定位点更稳）
    page.get_by_test_id("conn-tab-settings").click()
    # 字段标签经 a2a 域 i18n（fieldText）：总开关 + 入站端口必须渲染
    drawer.get_by_text("启用 A2A", exact=False).first.wait_for(timeout=15_000)
    drawer.get_by_text("入站端口", exact=False).first.wait_for(timeout=15_000)
    # 布尔字段渲染成开关（aria-checked 可读——面板真的活着，不是空壳）
    switches = drawer.locator('[role="switch"]')
    assert switches.count() >= 2
    # API 佐证：设置页数据面（configSchema/defaults）齐全
    detail = _api_get(port, "/api/connectors/a2a")
    props = (detail.get("configSchema") or {}).get("properties") or {}
    assert {"a2a_enabled", "inbound_enabled", "port"} <= set(props)


# ---- 3. 「对端节点」页签 + 行渲染 ---------------------------------------------


def test_peers_tab_renders_rows(app):
    page, port = app
    _add_peer(port)
    drawer = _open_a2a(page)
    # 自定义 kind 保留后端下发的双语 title（zh-CN 请求头 → 对端节点）
    tab = page.get_by_test_id("conn-tab-a2a-peers")
    tab.wait_for(state="visible", timeout=15_000)
    assert "对端节点" in tab.inner_text()
    tab.click()

    row = page.get_by_test_id(f"a2a-peer-row-{PEER_SLUG}")
    row.wait_for(state="visible", timeout=15_000)
    # 名称 / 端点 / 信任级别 / 连接状态（P0 未探测 → 未连接）
    assert PEER_NAME in row.inner_text()
    assert PEER_ENDPOINT in row.inner_text()
    assert "只读（默认）" in row.inner_text()
    assert "未连接" in row.inner_text()
    # 清单接口带 status（2026-11-18 扩展字段）
    peers = _api_get(port, "/api/a2a/peers").get("peers") or []
    assert peers and peers[0]["slug"] == PEER_SLUG
    assert peers[0]["status"] == "disconnected"


# ---- 4. 行内子配置编辑器可打开、编辑并保存 ------------------------------------


def test_peer_subconfig_editor_edit_and_save(app):
    page, port = app
    _add_peer(port)
    drawer = _open_a2a(page)
    pane = _open_peers_tab(page, drawer)
    _open_peer_editor(page, pane)

    row = page.get_by_test_id(f"a2a-peer-row-{PEER_SLUG}")
    # 信任级别 enum 字段：readonly → normal（option value 即后端枚举值）。
    # ⚠️ 必须精确匹配字段标题 span：能力级别行的描述「受信任级别约束」
    # 含相同子串，裸 has_text 会撞多行（strict mode violation）。
    title = row.locator("span").filter(has_text=re.compile("^信任级别$"))
    title.wait_for(state="visible", timeout=15_000)
    trust_select = title.locator(
        "xpath=ancestor::div[contains(@class,'grid')][1]"
    ).locator("select")
    trust_select.wait_for(state="visible", timeout=15_000)
    trust_select.select_option("normal")

    pane.get_by_role("button", name="保存", exact=True).click()
    pane.get_by_text("已应用", exact=False).first.wait_for(timeout=15_000)

    # 落盘复核：PATCH 打到了 peer 实例 id（PeerStore 合并写）
    cfg = _api_get(port, f"/api/connectors/a2a-{PEER_SLUG}").get("config") or {}
    assert cfg.get("trust") == "normal"


# ---- 5. 重连失败路径 + 删除确认流 ---------------------------------------------


def test_peer_reconnect_fail_and_delete_flow(app):
    page, port = app
    _add_peer(port)
    drawer = _open_a2a(page)
    pane = _open_peers_tab(page, drawer)

    # 重连探针：discard 端口必然不可达 → 失败提示（不伪装成功）
    pane.get_by_test_id(f"a2a-peer-reconnect-{PEER_SLUG}").click()
    pane.get_by_text("重连失败", exact=False).first.wait_for(timeout=30_000)
    peers = _api_get(port, "/api/a2a/peers").get("peers") or []
    assert peers[0]["status"] == "disconnected"

    # 删除：两步确认 → 行消失 + PeerStore 清空
    pane.get_by_test_id(f"a2a-peer-delete-{PEER_SLUG}").click()
    confirm = pane.get_by_test_id(f"a2a-peer-delete-confirm-{PEER_SLUG}")
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    page.get_by_test_id(f"a2a-peer-row-{PEER_SLUG}").wait_for(
        state="detached", timeout=15_000)
    pane.get_by_text("对端已删除", exact=False).first.wait_for(timeout=10_000)
    assert _api_get(port, "/api/a2a/peers").get("peers") == []
    # 动态实例同步注销（详情 API 404）
    try:
        _api_get(port, f"/api/connectors/a2a-{PEER_SLUG}")
        deleted = False
    except Exception:
        deleted = True
    assert deleted
