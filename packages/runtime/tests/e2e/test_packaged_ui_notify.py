"""Playwright e2e: OS 通知的触发边界，在真浏览器 + 真 packaged sidecar 上。

为什么 vitest 层不够（CLAUDE.md：前端交互改动必须配 packaged UI 用例）：
2026-11-14 的通知风暴根因是一个 switch 穿透——`token.delta` 经 fall through 进了
`case "user.question"` 的共享体，而 delta 帧没有 `id`，去重键退化成
`${sid}:${Date.now()}` 每帧唯一，于是**每个 token 弹一条「等你回答」**（单会话实测
1583 条，shell.log 里 77% 是通知流量）。纯函数测试原理上抓不到这种结构性错误；
reducer 层的 vitest（handleNotify.test.ts）能抓到，但仍需一层证明「整条链路在真
浏览器里接通」——桩接的 Tauri 桥、真 WS 帧、真偏好加载，任一环断了 vitest 都看不出来。

本文件覆盖四条链路级性质：
  1. delta 流**零**通知（P0-1 回归，浏览器级）；
  2. 真 ask_user **恰好一条**（防修过头把合法通知杀了）；
  3. 窗口可见但焦点在别处时**仍然通知**（P2：`isAppAttentive` 纳入 `hasFocus`；
     只看 `visibilityState` 会在这里静默——实测见
     scripts/probe-wkwebview-attention.README.md 步骤 05）；
  4. 总开关关掉 / privacy=titleOnly 时出口闸门真的生效（P1）。

注入说明（都不改动被测代码）：
  · Tauri 桥打桩——浏览器里没有壳，桩接 `__TAURI_INTERNALS__.invoke` 才能捕获
    `plugin:event|emit` 的 `ginno:notify` 载荷；同时让 `isDesktop()` 为真。
  · WS 帧放大——fake LLM 一个 turn 只吐 1 个 chunk，真实 provider 是几百个 delta
    帧。按 runtime 侧 `delta_coalesce` 的 **33ms 合帧节奏**排期复制。
    ⚠️ 不能用 `setTimeout(…, 0)`：那会把大批回调挤进同一毫秒，旧实现的
    `Date.now()` 去重键随之碰撞，80 帧只漏出个位数通知——严重低估。
  · 注意力状态可切换——`visibilityState` 与 `hasFocus()` 都由 `__setAttention`
    控制，用来精确制造「在看 / 切走但窗口在屏上 / 最小化」三种状态。

Skips gracefully when the binary isn't built, playwright isn't installed, or the
port is taken.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST

# 兄弟 packaged-UI 文件占了 8894-8899；每个 test 一个独立端口，避免前一个
# sidecar 拆卸慢导致后一个 skip（那正是最该跑的断言）。
PORT_MAIN = 8893
PORT_MASTER_OFF = 8892
PORT_PRIVACY = 8891

# 放大倍数：80 帧 × 33ms ≈ 2.6s，足够证明「一帧一条」而不至于拖慢用例
AMP = 80

Q1_HEADER = "通知边界 Q1"
Q2_HEADER = "通知边界 Q2"
OPTION = "就这个"

INIT_JS = r"""
(() => {
  window.__notifyLog = [];
  window.__wsStats = { deltaIn: 0, deltaAmp: 0, questionIn: 0 };

  // ---- Tauri 桥桩：isDesktop() 认 __TAURI_INTERNALS__；emit 走 invoke ----
  window.__TAURI_INTERNALS__ = {
    metadata: { currentWindow: { label: "main" }, currentWebview: { label: "main" } },
    invoke: async (cmd, args) => {
      if (cmd === "plugin:event|emit" && args && args.event === "ginno:notify") {
        window.__notifyLog.push(Object.assign({}, args.payload || {}, { t: Date.now() }));
      }
      return null;
    },
    transformCallback: (cb) => {
      const id = Math.random().toString(36).slice(2);
      window["__cb_" + id] = cb;
      return id;
    },
  };

  // ---- 可切换的注意力状态 ----
  // 默认「最小化/切走」：闸门全开，这样 delta 用例才是最强形式（连一条都不许有）。
  let VIS = "hidden";
  let FOCUS = false;
  Object.defineProperty(document, "visibilityState", { get: () => VIS, configurable: true });
  Object.defineProperty(document, "hidden", { get: () => VIS !== "visible", configurable: true });
  document.hasFocus = () => FOCUS;
  window.__setAttention = (vis, focus) => {
    VIS = vis; FOCUS = focus;
    document.dispatchEvent(new Event("visibilitychange"));
  };

  // ---- WS 帧放大：还原真实 provider 的 delta 帧量级 ----
  const AMP = __AMP__;
  const OrigWS = window.WebSocket;
  class PatchedWS extends OrigWS {
    constructor(...a) {
      super(...a);
      this.addEventListener("message", (e) => {
        let d; try { d = JSON.parse(e.data); } catch { return; }
        const ev = d && d.event;
        if (ev === "token.delta") {
          window.__wsStats.deltaIn++;
          const sock = this;
          for (let i = 0; i < AMP; i++) {
            // 按 33ms 合帧节奏**显式排期**，让 Date.now() 真的往前走
            setTimeout(() => {
              const h = sock.onmessage;
              if (typeof h !== "function") return;
              window.__wsStats.deltaAmp++;
              h.call(sock, new MessageEvent("message", {
                data: JSON.stringify(Object.assign({}, d, { content: "x" })),
              }));
            }, i * 33);
          }
        } else if (ev === "user.question") {
          window.__wsStats.questionIn++;
        }
      });
    }
  }
  window.WebSocket = PatchedWS;
})();
"""


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


def _settings(notify: dict | None = None) -> dict:
    base = {
        "default_provider": "custom",
        "bypass_permissions": True,
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
        "notifications": {
            "enabled": True, "sound": False, "sound_name": "Glass",
            # kinds_schema=2：告诉 loadNotifyPrefs 这些值是「用户的真选择」，
            # 不要跑 v1→v2 迁移把 workflow 拉回 true（见 notifyPrefs.migrateKinds）
            "kinds_schema": 2,
            "kinds": {"reply": True, "askUser": True, "failure": True,
                      "workflow": True, "schedule": True, "bgTask": True},
            "privacy": "full",
            "dnd": {"enabled": False, "from": "22:00", "to": "08:00"},
        },
    }
    if notify:
        merged = dict(base["notifications"])
        for k, v in notify.items():
            if k == "kinds":
                merged["kinds"] = {**merged["kinds"], **v}
            else:
                merged[k] = v
        base["notifications"] = merged
    return base


def _ask(header: str) -> dict:
    return {
        "content": "",
        "tool_calls": [
            {
                "name": "ask_user",
                "args": {"question": f"{header}：选哪个？", "options": [OPTION],
                         "header": header},
            }
        ],
    }


@pytest.fixture
def app(tmp_path, request):
    """Boot packaged sidecar + Chromium；yield (page, home)。

    间接参数化传一个 dict：``{"port": 8893, "notify": {...}, "scripts": [...]}``。
    """
    cfg = request.param
    port = cfg["port"]
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    if _port_open(port):
        pytest.skip(f"port {port} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(_settings(cfg.get("notify"))), encoding="utf-8"
    )
    scripts = tmp_path / "scripts.json"
    scripts.write_text(json.dumps(cfg["scripts"], ensure_ascii=False), encoding="utf-8")

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(port),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            # 必须在 goto 之前：init script 要赶在 app bundle 之前把
            # WebSocket / __TAURI_INTERNALS__ / visibilityState 都换掉
            page.add_init_script(INIT_JS.replace("__AMP__", str(AMP)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(2000)
            yield page, home
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def _send(page, text: str) -> None:
    ta = page.locator("textarea").first
    ta.click()
    ta.fill(text)
    ta.press("Enter")


def _emits(page) -> list[dict]:
    return page.evaluate("window.__notifyLog") or []


def _clear(page) -> None:
    page.evaluate("window.__notifyLog.length = 0")


def _wait_emits(page, n: int, timeout_ms: int = 20_000) -> list[dict]:
    """轮询等待 emit 数达到 n（≥）。落盘/事件传播有竞态，裸查一次会假阴性。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        got = _emits(page)
        if len(got) >= n:
            return got
        page.wait_for_timeout(250)
    return _emits(page)


# ── 主链路：delta 零通知 + 真 ask_user 恰好一条 + 注意力两态 ─────────────────
@pytest.mark.parametrize(
    "app",
    [{"port": PORT_MAIN,
      "scripts": [{"content": "第一段回复，纯文本，会被放大成 80 个 delta 帧。"},
                  _ask(Q1_HEADER),
                  {"content": "收到，Q1 已答。"},
                  _ask(Q2_HEADER)]}],
    indirect=True,
)
def test_delta_stream_silent_and_ask_user_notifies(app):
    page, _home = app

    # 闸门全开的最强形式：窗口不可见（等价于用户最小化/切走）
    page.evaluate("window.__setAttention('hidden', false)")

    # ① delta 流：P0-1 回归。修复前这里会是「一帧一条」= 80 条「等你回答」。
    _send(page, "帮我看看这三个方案的差异")
    page.wait_for_timeout(AMP * 33 + 4000)  # 等放大帧全部落地 + turn 收尾
    stats = page.evaluate("window.__wsStats")
    assert stats["deltaIn"] >= 1, "fake LLM 至少要吐一个 delta 帧，否则用例没测到东西"
    assert stats["deltaAmp"] >= AMP * 0.8, (
        f"帧放大没生效（deltaAmp={stats['deltaAmp']}），用例将失去意义"
    )
    deltas_only = [e for e in _emits(page)]
    assert deltas_only == [], (
        f"delta/tool 流不许产生任何 OS 通知，实际 {len(deltas_only)} 条："
        f"{[e.get('notify_kind') for e in deltas_only][:5]}"
    )

    # ②③ 窗口**可见但焦点在别处**（用户 ⌘Tab 切走、Ginno 窗口还在屏上）。
    # 这是 P2 把 hasFocus 纳入判据要修的场景：只看 visibilityState 会在这里
    # 判成「用户在看」而静默 —— 实测 WKWebView 此时确实报 visible
    # （scripts/probe-wkwebview-attention.README.md 步骤 05）。
    page.evaluate("window.__setAttention('visible', false)")
    _clear(page)
    _send(page, "那你建议先做哪个？问我一下")
    _wait_emits(page, 1)
    page.locator(f"text={Q1_HEADER}").first.wait_for(timeout=20_000)
    got = _emits(page)
    assert len(got) == 1, f"真 ask_user 应恰好一条通知，实际 {len(got)}：{got}"
    assert got[0].get("notify_kind") == "askUser", got[0]
    assert got[0].get("kind") == "session", "点击目标应是 session（点了跳该会话）"
    assert got[0].get("body"), "privacy=full 时正文不该被抹空"

    # ④ 用户正盯着（visible ∧ hasFocus）→ 不弹。先答掉 Q1 让 turn 收尾。
    page.locator(f"text={OPTION}").first.click()
    page.wait_for_timeout(2500)
    page.evaluate("window.__setAttention('visible', true)")
    _clear(page)
    _send(page, "再问我一次")
    page.locator(f"text={Q2_HEADER}").first.wait_for(timeout=20_000)
    page.wait_for_timeout(2500)
    assert _emits(page) == [], "用户正盯着该会话时不该弹 OS 通知"


# ── P1：总开关关掉，出口闸门必须真的拦住 ─────────────────────────────────────
@pytest.mark.parametrize(
    "app",
    [{"port": PORT_MASTER_OFF, "notify": {"enabled": False},
      "scripts": [_ask(Q1_HEADER)]}],
    indirect=True,
)
def test_master_switch_off_suppresses_ask_user(app):
    page, _home = app
    page.evaluate("window.__setAttention('hidden', false)")
    _send(page, "问我一下")
    page.locator(f"text={Q1_HEADER}").first.wait_for(timeout=20_000)
    page.wait_for_timeout(2500)
    assert _emits(page) == [], (
        "总开关关掉后任何分类都不该投递——修复前 lastwords 与失焦兜底两路"
        "完全不看总开关（审计文档 P1-1）"
    )


# ── P1：privacy=titleOnly 时正文必须在出口被抹空 ─────────────────────────────
@pytest.mark.parametrize(
    "app",
    [{"port": PORT_PRIVACY, "notify": {"privacy": "titleOnly"},
      "scripts": [_ask(Q1_HEADER)]}],
    indirect=True,
)
def test_privacy_title_only_strips_body(app):
    page, _home = app
    page.evaluate("window.__setAttention('hidden', false)")
    _send(page, "问我一下")
    _wait_emits(page, 1)
    page.locator(f"text={Q1_HEADER}").first.wait_for(timeout=20_000)
    got = _emits(page)
    assert len(got) == 1, f"应恰好一条，实际 {len(got)}"
    assert got[0].get("body") == "", (
        f"privacy=titleOnly 时正文必须为空，实际 {got[0].get('body')!r}"
        "——修复前只有 4/7 个站点自己裁了正文（审计文档 P1-1）"
    )
    assert got[0].get("title"), "标题应保留（titleOnly 不是静音）"
