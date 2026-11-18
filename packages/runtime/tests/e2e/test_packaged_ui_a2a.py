"""Packaged-UI e2e: A2A 连接器（a2a-connector-design.md §6/§12.C/§13.1）。

2026-11-12 起的规矩（CLAUDE.md 红线）：前端交互改动必须配 packaged UI 用例——
vitest/tsc 抓不到渲染层断裂（@ 菜单全灭三天才被发现的教训）。A2A 连接器前端
已有组件级 vitest（A2aInboundCard.test.tsx / A2aPeerAddModal.test.tsx），但
渲染层 + 真后端 action 链路零覆盖，本文件补上（设计 §13.1 用例 30）。

与 test_packaged_ui_team.py / test_packaged_ui_notify.py 同基建：dist sidecar +
真实 Chromium，GINNO_HOME 隔离到 tmp_path。端口用 bind(0) 随机取空闲口，
不碰 8787，也不占兄弟文件的 8891-8899 段。语言钉 zh-CN——断言词
（「重新粘贴」/「第三方」/「信任」）取自 messages/zh-CN/a2a.json，
与 team 用例钉 en 同一基线做法。

覆盖五条链路：
1. 入站开关确认流：开启需二次确认，取消开关不变，确认生效——断言 DOM
   aria-checked 而非只断言 API；关闭不需确认（§12.C-4 只约束危险操作）。
2. 地址明文（§12.C-2）：完整 26 字符 base32 token + #fp 片段，无掩码字符
   ••••，配「等同密码」提示；DOM 与后端 extra 上报一致。
3. 重置确认流：文案含「重新粘贴」，取消地址不变，确认后 token 轮换而
   指纹不变（reset 只换 token，identity 不轮换）。
4. 接入向导四步（§6.1）：假地址 → Card 预览失败走「未发布 Agent Card」
   手动填表兜底，**第三方信任提示必须出现**（硬要求），能走到步骤 4 并完成接入。
5. R3 边界（§11.2 / §13.1 用例 28 的 UI 侧印证）：A2A 详情页没有任何指向
   settings/mcp/skills 的入口——R3 路由根本不注册，UI 也不留入口。

2026-11-18 晚用户实测踩坑补两条（入站 toggle UX 陷阱）：
6. 总开关（settings 页签 a2a_enabled）关 → 入站 toggle 必须物理禁用
   （disabled + aria-disabled + data-master-off），dispatch_event 合成
   click 穿透也不许发 PATCH / 不弹确认框——旧版可点可保存但运行时
   永不生效（apply_config 无热重载），用户以为卡死。
7. PATCH 成功后必须出现「重启 Ginno 后生效」行内提示——服务起停实际
   要重启 sidecar，UI 不能沉默。

总开关默认关（default_config fail-closed），所以走开关流的既有用例
（1/2/3 与新 7）先经 `_enable_master`（API PATCH + reload，确定性地
让列表带上 a2a_enabled=true 再开抽屉）；用例 6 反向利用默认态。

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

# 地址形态（§6.2）：https://<bind_ip>:<port>/a2a/<26 字符 base32 token>#fp=<指纹>。
# 默认 loopback:8790；P0 骨架只派生地址不起 listener，不会真占该端口。
_ADDR_RE = re.compile(
    r"^https://127\.0\.0\.1:8790/a2a/([A-Za-z2-7]{26})#fp=([A-Za-z2-7]{26})$"
)

# 向导用假对端：loopback 的 9 端口（discard）必然不可达 → 预览必失败，
# 走 §6.1 第 1 步失败态的手动填表兜底；路径不带 token → 凭据步（3）不跳过，
# 四步全走。
FAKE_PEER = "https://127.0.0.1:9/a2a"


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
    # language pin zh-CN：断言词依赖 locale 固定（与 team 用例钉 en 同基线）。
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


def _api(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def _patch_config(port: int, payload: dict) -> dict:
    """PATCH 枢纽配置（与前端 patchConnectorConfig 同端点）。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/connectors/a2a/config",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


@pytest.fixture
def app(tmp_path):
    """Boot packaged sidecar + Chromium，yield (page, port)。"""
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
            page.goto(f"http://127.0.0.1:{port}/connectors", wait_until="load")
            yield page, port
            browser.close()
    finally:
        sc.stop_sidecar(proc)


# ---- 共用助手 ---------------------------------------------------------------


def _open_a2a(page):
    """从连接器列表点开 A2A 枢纽详情抽屉，返回 drawer locator。"""
    card = page.locator('[role="button"]').filter(has_text="A2A").first
    card.wait_for(state="visible", timeout=30_000)
    card.click()
    drawer = page.locator('[role="dialog"][aria-label*="A2A"]')
    drawer.wait_for(state="visible", timeout=15_000)
    # 等 zh-CN locale 生效（settings 异步加载；后续断言词依赖它）
    drawer.get_by_text("入站 · 被其他 Agent 连接", exact=False).wait_for(timeout=15_000)
    page.get_by_test_id("a2a-inbound-toggle").wait_for(state="visible", timeout=15_000)
    return drawer


def _wait_toggle(page, expected: str, timeout: float = 15) -> None:
    """轮询等开关 DOM 状态翻转（PATCH + onReload 有竞态，裸查一次会假阴性）。"""
    toggle = page.get_by_test_id("a2a-inbound-toggle")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if toggle.get_attribute("aria-checked") == expected:
            return
        page.wait_for_timeout(250)
    raise AssertionError(f"inbound toggle aria-checked 未翻到 {expected}")


def _enable_inbound(page) -> None:
    """走完整 UI 确认流开启入站（开关 → 二次确认 → 生效）。"""
    page.get_by_test_id("a2a-inbound-toggle").click()
    confirm = page.get_by_test_id("a2a-inbound-enable-confirm")
    confirm.wait_for(state="visible", timeout=10_000)
    confirm.click()
    _wait_toggle(page, "true")


def _wait_address(page, timeout: float = 25) -> str:
    """轮询等接入地址渲染（identity 生成 + meta 上报 + onReload 全链路）。"""
    loc = page.get_by_test_id("a2a-inbound-address")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if loc.count():
            txt = (loc.inner_text() or "").strip()
            if txt:
                return txt
        page.wait_for_timeout(300)
    raise AssertionError("接入地址未渲染（后端 extra.inboundAddress 未上报？）")


def _enable_master(page, port: int) -> None:
    """打开 A2A 总开关（a2a_enabled）——入站 toggle 的前置条件。

    API PATCH + page.reload：连接器列表有 5s 轮询 + WS 推送，但为让
    抽屉首开就带上 a2a_enabled=true（无竞态），直接重载页面最确定。
    """
    _patch_config(port, {"a2a_enabled": True})
    page.reload(wait_until="load")


# ---- 1. 入站开关确认流 -------------------------------------------------------


def test_inbound_toggle_confirm_flow(app):
    page, port = app
    _enable_master(page, port)
    _open_a2a(page)
    toggle = page.get_by_test_id("a2a-inbound-toggle")
    # §13.1 用例 14：入站默认关闭
    assert toggle.get_attribute("aria-checked") == "false"

    toggle.click()
    confirm = page.get_by_test_id("a2a-inbound-enable-confirm")
    confirm.wait_for(state="visible", timeout=10_000)
    # 二次确认必须带安全提示（§12.C-4 危险操作显式化）
    assert page.get_by_text("开启入站接入？", exact=False).count() >= 1

    # 取消 → 开关不变（DOM 主断言 + API 佐证未落盘）
    page.get_by_role("button", name="取消", exact=True).click()
    confirm.wait_for(state="detached", timeout=10_000)
    assert toggle.get_attribute("aria-checked") == "false"
    stored = _api(port, "/api/connectors/a2a").get("config") or {}
    assert not stored.get("inbound_enabled"), "取消确认不应写入配置"

    # 再开 → 确认 → 生效（断言 DOM 状态，不只断言 API）
    toggle.click()
    page.get_by_test_id("a2a-inbound-enable-confirm").click()
    _wait_toggle(page, "true")
    eff = _api(port, "/api/connectors/a2a").get("effective") or {}
    assert eff.get("inbound_enabled") is True

    # 关闭不需二次确认（§12.C-4 只约束开启/重置这类危险操作）
    toggle.click()
    _wait_toggle(page, "false")
    assert page.get_by_test_id("a2a-inbound-enable-confirm").count() == 0


# ---- 1b. 总开关关 → 入站 toggle 物理禁用（2026-11-18 UX 陷阱） ---------------


def test_master_off_disables_inbound_toggle(app):
    """总开关未开（默认态）：toggle 必须 disabled + aria-disabled，旁路点击无效。

    旧版 toggle 可点可 PATCH——配置写进去了但 apply_config 无热重载、
    运行时永不生效，黄条「A2A 总开关未开启」常驻，用户以为卡死。
    """
    page, port = app
    drawer = _open_a2a(page)
    toggle = page.get_by_test_id("a2a-inbound-toggle")

    # 三重禁用信号：原生 disabled、aria-disabled、data-master-off 钩子
    assert toggle.is_disabled(), "总开关关时入站 toggle 必须原生 disabled"
    assert toggle.get_attribute("aria-disabled") == "true"
    assert toggle.get_attribute("data-master-off") == "true"
    # 因果说明仍在（黄条）：告诉用户去哪里开总开关
    assert drawer.get_by_text("A2A 总开关未开启", exact=False).count() >= 1

    # dispatch_event 直接触发 click 事件（原生 disabled 拦用户交互，但
    # 合成事件/编程调用能穿透）——组件层 inboundToggleAction 的 blocked
    # 门必须兜住：不许发 PATCH、不许弹确认框。
    toggle.dispatch_event("click")
    page.wait_for_timeout(500)
    assert toggle.get_attribute("aria-checked") == "false"
    assert page.get_by_test_id("a2a-inbound-enable-confirm").count() == 0
    stored = _api(port, "/api/connectors/a2a").get("config") or {}
    assert not stored.get("inbound_enabled"), "总开关关时旁路点击也不许写入配置"

    # 总开关打开（API + reload）后 toggle 恢复可用——禁用态不许变成死锁
    _enable_master(page, port)
    drawer = _open_a2a(page)
    toggle = page.get_by_test_id("a2a-inbound-toggle")
    assert not toggle.is_disabled()
    assert toggle.get_attribute("data-master-off") is None


# ---- 1c. PATCH 成功 → 「重启后生效」提示（2026-11-18 UX 陷阱） ---------------


def test_toggle_patch_shows_restart_notice(app):
    """开关 PATCH 成功后必须明示「配置已保存，重启 Ginno 后生效」。

    apply_config 无热重载（P0 设计），服务起停实际要重启 sidecar——
    旧版 UI 只翻开关状态，用户以为已立即生效。
    """
    page, port = app
    _enable_master(page, port)
    _open_a2a(page)

    # 关→开（走确认流）：成功后行内提示出现
    page.get_by_test_id("a2a-inbound-toggle").click()
    page.get_by_test_id("a2a-inbound-enable-confirm").click()
    _wait_toggle(page, "true")
    page.get_by_text("配置已保存，重启 Ginno 后生效", exact=False).wait_for(
        timeout=10_000
    )
    # 提示是成功态（绿字）而非错误文案
    eff = _api(port, "/api/connectors/a2a").get("effective") or {}
    assert eff.get("inbound_enabled") is True

    # 开→关（免确认直发）：同样要给提示——两个方向都不许沉默。
    # expect_response 钉住这次 PATCH 往返：旧提示在 click 时已被同步清掉
    # （applyToggle 开头 setMsg(null)），wait_for 通过的必然是新提示。
    with page.expect_response(
        lambda r: "/api/connectors/a2a/config" in r.url
        and r.request.method == "PATCH"
    ):
        page.get_by_test_id("a2a-inbound-toggle").click()
    _wait_toggle(page, "false")
    page.get_by_text("配置已保存，重启 Ginno 后生效", exact=False).wait_for(
        timeout=10_000
    )
    stored = _api(port, "/api/connectors/a2a").get("config") or {}
    assert stored.get("inbound_enabled") is False


# ---- 2. 地址明文（§12.C-2） --------------------------------------------------


def test_inbound_address_plaintext_no_mask(app):
    page, port = app
    _enable_master(page, port)
    _open_a2a(page)
    _enable_inbound(page)
    addr = _wait_address(page)

    # 明文是刻意设计：无掩码字符（•••• 或单个 • 都不许有）
    assert "••••" not in addr and "•" not in addr, f"接入地址被掩码: {addr!r}"
    # 完整 token（26 字符 base32）+ #fp 片段，一个字符都不能少
    m = _ADDR_RE.fullmatch(addr)
    assert m, f"地址应含完整 26 字符 token 与 #fp 片段: {addr!r}"
    assert "#fp=" in addr
    # 明文必须配「等同密码」提示（§12.C-2 的配对要求）
    assert page.get_by_text("此地址等同密码", exact=False).count() >= 1
    assert page.get_by_test_id("a2a-inbound-copy").is_visible()
    assert page.get_by_test_id("a2a-inbound-reset").is_visible()
    # DOM 与后端 extra 一致——前端不许自行二次掩码
    extra = _api(port, "/api/connectors/a2a").get("extra") or {}
    assert extra.get("inboundAddress") == addr


# ---- 3. 重置确认流 -----------------------------------------------------------


def test_inbound_reset_confirm_rotates_token(app):
    page, port = app
    _enable_master(page, port)
    _open_a2a(page)
    _enable_inbound(page)
    addr1 = _wait_address(page)
    m1 = _ADDR_RE.fullmatch(addr1)
    assert m1, addr1

    page.get_by_test_id("a2a-inbound-reset").click()
    reset_confirm = page.get_by_test_id("a2a-inbound-reset-confirm")
    reset_confirm.wait_for(state="visible", timeout=10_000)
    # 后果说明必须含「重新粘贴」（§12.C-4）
    assert page.get_by_text("重新粘贴", exact=False).count() >= 1

    # 取消 → 地址不变
    page.get_by_role("button", name="取消", exact=True).click()
    reset_confirm.wait_for(state="detached", timeout=10_000)
    assert page.get_by_test_id("a2a-inbound-address").inner_text().strip() == addr1

    # 确认 → token 轮换、指纹不变（reset 只换 token，identity 不轮换）
    page.get_by_test_id("a2a-inbound-reset").click()
    page.get_by_test_id("a2a-inbound-reset-confirm").click()
    deadline = time.time() + 25
    addr2 = addr1
    while time.time() < deadline:
        addr2 = page.get_by_test_id("a2a-inbound-address").inner_text().strip()
        if addr2 != addr1:
            break
        page.wait_for_timeout(300)
    assert addr2 != addr1, "重置确认后地址未变化"
    m2 = _ADDR_RE.fullmatch(addr2)
    assert m2, addr2
    assert m2.group(1) != m1.group(1), "重置后 token 未轮换"
    assert m2.group(2) == m1.group(2), "指纹应不变（identity 不轮换）"
    assert "•" not in addr2, f"新地址被掩码: {addr2!r}"


# ---- 4. 接入向导四步 + 第三方信任提示（§6.1） --------------------------------


def test_peer_wizard_four_steps_trust_warning(app):
    page, _port = app
    _open_a2a(page)
    page.get_by_test_id("a2a-peer-add").click()
    dlg = page.get_by_role("dialog", name="接入外部 Agent", exact=True)
    dlg.wait_for(state="visible", timeout=10_000)

    # 步骤 1：粘贴地址
    assert dlg.get_by_text("第 1 步 / 共 4 步", exact=False).count() >= 1
    dlg.locator("textarea").fill(FAKE_PEER)
    dlg.get_by_role("button", name="下一步", exact=True).click()

    # 步骤 2：Card 预览——第三方信任提示是 §6.1 硬要求
    dlg.get_by_text("Agent Card 预览", exact=False).wait_for(timeout=10_000)
    trust = dlg.get_by_text("第三方", exact=False).first
    trust.wait_for(state="visible", timeout=10_000)
    assert "信任" in trust.inner_text(), "信任提示文案缺「信任」字样"
    # 假地址必然不可达 → 「未发布 Agent Card」手动填表兜底（§6.1 失败态）
    dlg.get_by_text("未发布 Agent Card", exact=False).wait_for(timeout=30_000)
    # 预览失败不许把信任提示吞掉
    assert dlg.get_by_text("第三方", exact=False).first.is_visible()
    dlg.get_by_placeholder("例如：报销助手").fill("E2E 假对端")
    dlg.get_by_role("button", name="下一步", exact=True).click()

    # 步骤 3：凭据（地址不带 token → 不跳过）
    dlg.get_by_text("凭据", exact=True).wait_for(timeout=10_000)
    dlg.get_by_role("button", name="下一步", exact=True).click()

    # 步骤 4：确认接入（摘要含端点，信任提示再次出现）
    dlg.get_by_text("第 4 步 / 共 4 步", exact=False).wait_for(timeout=10_000)
    assert dlg.get_by_text(FAKE_PEER, exact=False).count() >= 1
    assert dlg.get_by_text("第三方", exact=False).first.is_visible()
    dlg.get_by_role("button", name="确认接入", exact=True).click()

    # 接入完成：向导关闭 + 成功提示（后端 a2a_add_peer 真注册）
    dlg.wait_for(state="detached", timeout=20_000)
    page.get_by_text("已接入 E2E 假对端", exact=False).wait_for(timeout=10_000)


# ---- 5. R3 边界：无 settings/mcp/skills 入口（§11.2） ------------------------


def test_r3_no_settings_mcp_skills_entries(app):
    page, port = app
    drawer = _open_a2a(page)

    # 后端声明：枢纽 detailView 不得有 mcp/skills/install 类页签
    detail = _api(port, "/api/connectors/a2a")
    kinds = {t.get("kind") for t in (detail.get("detailView") or {}).get("tabs") or []}
    assert not kinds & {"mcp", "skills", "install"}, kinds

    # UI 侧：逐页签扫描——无指向 settings/mcp/skills 路由的链接，
    # 无 MCP/技能管理入口。R3（MCP 配置/skills/hooks）路由根本不注册，
    # UI 也不留入口（§13.1 用例 28 的 UI 侧印证）。
    # 注：枢纽自己的「设置」页签是 A2A 自身配置，合法；这里禁的是
    # 指向全局 settings/mcp/skills 页面的导航入口。
    for tab_name in ("概览", "设置", "活动"):
        # 页签按钮带 border-b-2；概览正文里也有「设置」按钮（打开本枢纽
        # 设置页签），裸 name 定位会撞 strict mode，按类名区分。
        tab_btn = drawer.locator("button.border-b-2").filter(
            has_text=re.compile(f"^{tab_name}$")
        )
        if tab_btn.count():
            tab_btn.click()
            page.wait_for_timeout(400)
        hrefs = drawer.locator("a[href]").evaluate_all(
            "els => els.map(e => e.getAttribute('href') || '')"
        )
        for href in hrefs:
            assert not re.search(r"/(settings|mcp|skills)(/|\?|#|$)", href), (
                f"A2A 详情页存在指向 {href} 的入口（R3 边界）"
            )
        texts = drawer.locator("button, a").evaluate_all(
            "els => els.map(e => e.textContent || '')"
        )
        for txt in texts:
            assert not re.search(r"MCP|技能|skill", txt, re.IGNORECASE), (
                f"A2A 详情页存在 MCP/技能类入口: {txt!r}（R3 边界）"
            )

    # 所有 a2a-* 定位点本身不得是导航链接
    tags = page.locator("[data-testid^='a2a-']").evaluate_all(
        "els => els.map(e => e.tagName)"
    )
    assert "A" not in tags, tags


# ---- 6. 未保存更改不得把用户困在详情页（2026-11-16 事故回归）------------------
#
# 事故：进 A2A 连接器后**切不到任何其他页面**。根因是 canLeave() 同步调
# window.confirm，而 Tauri WKWebView 屏蔽原生 JS 对话框、confirm() 恒返回
# false → 关闭与切 tab 全被静默拒绝。vitest/tsc 抓不到（它们不跑 WKWebView），
# 故按 CLAUDE.md 规矩补浏览器级用例。
#
# 这里跑的是 Chromium（原生 confirm 可用），所以断言重点不是「confirm 返回什么」，
# 而是**确认必须走应用内模态**（role=dialog + 中文标题），且取消/放弃两条路径
# 都真的能走通——只要有人改回 window.confirm，模态断言立刻红。


def _open_a2a_settings_dirty(page):
    """开 A2A 详情 → 设置 tab → 改 endpoint 造 dirty，返回 drawer。"""
    drawer = _open_a2a(page)
    drawer.get_by_role("button", name="设置", exact=True).first.click()
    # endpoint 是 string 字段，填个值即可让 ConfigEditor 报 dirty
    box = drawer.get_by_placeholder("https://host:port/a2a#fp=...").first
    if not box.count():
        # placeholder 用的是 description，退化为按 label 找文本框
        box = drawer.locator('input[type="text"], textarea').first
    box.wait_for(state="visible", timeout=15_000)
    box.fill("https://example.invalid/a2a")
    page.wait_for_timeout(400)  # 等 onDirtyChange 上报
    return drawer


def test_unsaved_changes_use_in_app_modal_not_native_confirm(app):
    """dirty 后点关闭：必须弹应用内模态，取消后仍留在详情页。"""
    page, _port = app
    drawer = _open_a2a_settings_dirty(page)

    # 原生 confirm 若被调用会阻塞/自动 dismiss——装个探针钉住它绝不被调
    page.evaluate(
        "() => { window.__nativeConfirmCalls = 0;"
        " const orig = window.confirm;"
        " window.confirm = (...a) => { window.__nativeConfirmCalls++;"
        " return orig ? orig.apply(window, a) : false; }; }"
    )

    drawer.get_by_title("关闭").click()

    modal = page.get_by_role("dialog", name="放弃未保存的更改？")
    modal.wait_for(state="visible", timeout=10_000)
    assert modal.get_by_text("有未保存的更改，确定放弃吗？").count() > 0

    calls = page.evaluate("() => window.__nativeConfirmCalls")
    assert calls == 0, f"仍在调原生 window.confirm（{calls} 次）——Tauri 下会把用户困住"

    # 取消 → 模态消失、详情页仍在（未被误关）
    modal.get_by_role("button", name="取消").click()
    modal.wait_for(state="detached", timeout=10_000)
    drawer.wait_for(state="visible", timeout=5_000)


def test_unsaved_discard_actually_leaves_the_page(app):
    """dirty 后点关闭 → 放弃并离开：详情页必须真的关掉（事故里永远关不掉）。"""
    page, _port = app
    drawer = _open_a2a_settings_dirty(page)
    drawer.get_by_title("关闭").click()

    modal = page.get_by_role("dialog", name="放弃未保存的更改？")
    modal.wait_for(state="visible", timeout=10_000)
    modal.get_by_role("button", name="放弃并离开").click()

    # 关键断言：抽屉真的消失了——旧实现恒 false，这里会超时红
    drawer.wait_for(state="detached", timeout=10_000)
    page.get_by_test_id("a2a-inbound-toggle").wait_for(state="detached", timeout=10_000)
    # 回到连接器列表，能继续点别的卡片（导航未被卡死）
    page.locator('[role="button"]').first.wait_for(state="visible", timeout=10_000)


def test_dirty_tab_switch_asks_then_proceeds(app):
    """dirty 后切 tab 也要走应用内模态，确认后真的切过去。"""
    page, _port = app
    drawer = _open_a2a_settings_dirty(page)
    drawer.get_by_role("button", name="概览", exact=True).first.click()

    modal = page.get_by_role("dialog", name="放弃未保存的更改？")
    modal.wait_for(state="visible", timeout=10_000)
    modal.get_by_role("button", name="放弃并离开").click()
    modal.wait_for(state="detached", timeout=10_000)
    # 切成功后入站卡（概览 tab 内容）重新可见
    page.get_by_test_id("a2a-inbound-toggle").wait_for(state="visible", timeout=10_000)
