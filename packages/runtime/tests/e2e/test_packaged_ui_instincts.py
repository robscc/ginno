"""Packaged-UI e2e: 本能 P0 可见化（2026-11-15）。

为什么 vitest 层不够（CLAUDE.md：前端交互改动必须配 packaged UI 用例）：
本次修的是「管理页空转」——observer 默认禁用 + 观察 feed 渲染不出纠偏内容
+ 无分析状态展示，三个缺口都在渲染/接线层，纯函数测试抓不到。本文件在真
浏览器 + 真 packaged sidecar 上证明四条链路级性质：

1. 观察 feed 真的显示 user_correction 的正文（旧实现只渲染 tool/args_repr，
   纠偏行一片空白——用户报「看不到总结了什么」的直接根因）；
2. 分析器状态行可见（位点/待分析/判定模型/门槛），不再 invisible；
3. 「立即分析」走通 force 全链路：fake LLM 判定 → pending 规则落盘 →
   页面出卡片 → 一键采纳转 active（纠偏门槛 3 条即触发，不再共用 20）；
4. 设置 → 智能体 → 本能 tab 存在且保存真的写穿 settings.json
   （此前 instincts 段没有任何 UI 入口）。

噪声守卫顺带验证：子代理 brief 观察不喂 LLM（脚本只给 3 条判定回复，
第 4 条噪声若被喂进去会拿到空回复/错位，created/reinforced 计数即穿帮）。

Skips gracefully when the binary isn't built, playwright isn't installed, or
the port is taken.
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
PORT = 8890  # 兄弟 packaged-UI 文件占 8891-8899

CORRECTION_TEXT = "后续不要帮我决定了 你只是做一下总结"
NOISE_TEXT = "<ginno_subagent_result session=\"x\">不要改范围外文件</ginno_subagent_result>"
RULE_TRIGGER = "when asked to decide"
RULE_BEHAVIOR = "only summarize and route, never decide for the user"


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


def _seed_observations(home: Path) -> None:
    """3 条真实纠偏 + 1 条子代理噪声（存量噪声分析侧同口径过滤）。"""
    inst_dir = home / "instincts"
    inst_dir.mkdir(parents=True, exist_ok=True)
    events = []
    for i in range(3):
        events.append({
            "type": "user_correction", "reason": "correction-word",
            "before": "", "after": CORRECTION_TEXT,
            "session": f"s{i}", "project": "", "turn": "",
            "ts": f"2026-11-15T0{i}:00:00+08:00",
        })
    events.append({
        "type": "user_correction", "reason": "correction-word",
        "before": "", "after": NOISE_TEXT,
        "session": "s-noise", "project": "", "turn": "",
        "ts": "2026-11-15T04:00:00+08:00",
    })
    (inst_dir / "observations.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def _judge_scripts(path: Path) -> None:
    """observer 判定回复 ×3（每条真实纠偏一次；噪声不应消耗脚本）。"""
    reply = json.dumps({
        "is_rule": True, "trigger": RULE_TRIGGER, "behavior": RULE_BEHAVIOR,
        "domain": "user-preference", "scope": "global",
    })
    path.write_text(json.dumps([{"content": reply} for _ in range(3)]))


def test_packaged_ui_instincts_visibility_and_analyze(tmp_path):
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
    _seed_observations(home)
    scripts = tmp_path / "scripts.json"
    _judge_scripts(scripts)

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
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/instincts", wait_until="load")
            page.wait_for_timeout(1500)

            # 1) 观察 feed 显示纠偏正文（旧实现这里一片空白）
            feed = page.locator("section").filter(has_text="Observations").last
            assert feed.count() >= 1, "observation feed section should exist"
            assert CORRECTION_TEXT in feed.inner_text(), (
                "user_correction content must be visible in the feed"
            )

            # 2) 分析器状态行：位点 0/4 · 待分析 4 · 跟随主模型
            status = page.locator("section").filter(has_text="Analyzer status").last
            assert status.count() >= 1, "analyzer status row should exist"
            stext = status.inner_text()
            assert "0/4" in stext and "4 pending" in stext, f"offset wrong: {stext}"
            assert "follows main model" in stext, f"model resolution wrong: {stext}"

            # 3) 立即分析：force 全链路 → 1 新建 + 2 强化（噪声不喂 LLM）
            page.get_by_role("button", name="Analyze now").click()
            page.wait_for_timeout(2500)
            body = page.inner_text("body")
            assert "1 created" in body and "2 reinforced" in body, (
                f"analyze result message missing/wrong: {body[:800]}"
            )
            # pending 规则卡片出现，可一键采纳
            card = page.locator("div.rounded-lg").filter(has_text=RULE_TRIGGER).last
            assert card.count() >= 1, "new pending rule card should render"
            assert "Pending" in card.inner_text()
            card.get_by_role("button", name="Adopt").click()
            page.wait_for_timeout(800)
            card = page.locator("div.rounded-lg").filter(has_text=RULE_TRIGGER).last
            assert "Active" in card.inner_text(), "adopt should flip pending → active"
            # 位点前进到 4/4
            status = page.locator("section").filter(has_text="Analyzer status").last
            assert "4/4" in status.inner_text() and "0 pending" in status.inner_text()

            # 4) 设置 tab：存在、可改、保存写穿 settings.json
            page.goto(
                f"http://127.0.0.1:{PORT}/settings/agents/instincts",
                wait_until="load",
            )
            page.wait_for_timeout(1200)
            assert page.get_by_text("Enable instincts").count() >= 1, (
                "settings → agents → instincts tab should render"
            )
            enabled = page.locator("label").filter(has_text="Enable instincts").locator("input")
            assert enabled.is_checked()
            enabled.click()  # 关掉
            page.get_by_role("button", name="Save").click()
            page.wait_for_timeout(800)
            import urllib.request

            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/api/settings", timeout=5
            ) as r:
                saved = json.loads(r.read().decode())
            assert saved.get("instincts", {}).get("enabled") is False, (
                "save must write through to settings.json instincts block"
            )
            # 其它设置块未被误清空（整文件替换守卫）
            assert saved.get("default_provider") == "custom"
            browser.close()
    finally:
        sc.stop_sidecar(proc)