"""Packaged-UI e2e: lastwords 恢复提示的决策闭环（2026-11-16 交互补齐）。

链路验收（真浏览器 + 真 packaged sidecar + 真 /api/lastwords/resume）：
  1. 重启后挂起的遗言 → 通知卡呈现**真实请求摘要**与**被中断后台任务数**，
     带「Resume / Discard」两个决策按钮，且**常驻不自动消失**（duration:0）；
  2. 点 Resume → 真 POST /api/lastwords/resume（body 只带 session_id）→
     后端 headless 续跑（fake LLM）→ runtime.lastwords_restored 回执进通知；
     条目被消费（再 resume 返回 no_pending）、遗言文件被删；
  3. 点 Discard → 真 POST 同端点 dismiss:true → 条目消费 + 文件删除。

为什么 vitest 层不够（CLAUDE.md：前端交互改动必须配 packaged UI 用例）：
handleLastwords.test.ts 桩掉了 API 出口，证明不了「按钮 → fetch → 真后端
状态迁移 → 回执」整条链在真浏览器里接通；i18n 插值（head/bgInterrupted）
也只有真 next-intl 渲染才能验证。

注入说明（都不改动被测代码）：
  · 遗言快照与 interrupted 任务索引是**真实落盘**的（home/lastwords/*.json、
    home/tasks/index.json），重启后由真 recover_lastwords 扫描注入并挂起。
  · 启动广播（lifespan +2s）与浏览器 WS 重连存在结构性竞态（前端重连间隔
    3s > 2s），真实帧可能先于 socket 就绪发出。INIT_JS 记录真实到达的
    runtime.lastwords_pending 帧；若窗口内没等到，就按后端契约原样补投一帧
    （payload 与 lastwords.py 广播字段逐一对应）。**决策链路本身全真**：
    按钮点击走真 fetch、真端点、真状态迁移。
  · 后台任务数断言非零：索引里种两条 interrupted 记录（origin_session 指向
    被测会话），reconcile_bg_tasks 实时核对后仍保留 → bg_interrupted=2。

Skips gracefully when the binary isn't built, playwright isn't installed, or the
port is taken.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST

# 兄弟 packaged-UI 文件占了 8890-8899；本文件用 8887/8886。
PORT_RESUME = 8887
PORT_DISMISS = 8886

HEAD_TEXT = "Summarize the quarterly report into slides"
USER_MSG = "quarterly report slides"

INIT_JS = r"""
(() => {
  window.__sockets = [];
  window.__lwReal = null;
  const OrigWS = window.WebSocket;
  class PatchedWS extends OrigWS {
    constructor(...a) {
      super(...a);
      window.__sockets.push(this);
      this.addEventListener("message", (e) => {
        let d; try { d = JSON.parse(e.data); } catch { return; }
        if (d && d.event === "runtime.lastwords_pending") window.__lwReal = d;
      });
    }
  }
  window.WebSocket = PatchedWS;
  // 按后端契约补投一帧（仅当真实广播先于 socket 就绪时用到）。
  window.__injectLw = (payload) => {
    const frame = JSON.stringify(Object.assign({ event: "runtime.lastwords_pending" }, payload));
    for (const s of window.__sockets) {
      if (s.readyState !== 1) continue;
      const h = s.onmessage;
      if (typeof h === "function") h.call(s, new MessageEvent("message", { data: frame }));
    }
  };
  window.__liveSockets = () => window.__sockets.filter((s) => s.readyState === 1).length;
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


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _api(port: int, path: str, body: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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


def _env(home: Path, port: int) -> dict:
    return dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_RUNTIME_PORT=str(port),
    )


def _seed_lastwords(home: Path, sid: str) -> dict:
    """真实落盘：interrupted 任务索引 + 遗言快照；返回契约 payload。"""
    now = int(time.time())
    tasks = home / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "index.json").write_text(json.dumps({
        "t-dead-1": {"id": "t-dead-1", "status": "interrupted", "origin_session": sid,
                      "command": "sleep 999", "title": "tunnel"},
        "t-dead-2": {"id": "t-dead-2", "status": "interrupted", "origin_session": sid,
                      "command": "sleep 999", "title": "ci watch"},
    }))
    lw = home / "lastwords"
    lw.mkdir(parents=True, exist_ok=True)
    snap = {
        "session_id": sid,
        "project": "default",
        "reason": "signal",
        "user_request_head": HEAD_TEXT,
        "last_agent_message": "working on the slides…",
        "running_tools": [],
        "bg_tasks": [],
        "bg_interrupted": ["t-dead-1", "t-dead-2"],
        "ts": now,
        "effort": {"tool_calls": 6, "started_at": now - 420},
    }
    (lw / f"{sid}.json").write_text(json.dumps(snap))
    # 与 lastwords.py 广播字段逐一对应的 payload（补投用）
    return {
        "session_id": sid,
        "head": HEAD_TEXT[:160],
        "user_request_head": HEAD_TEXT[:160],
        "reason": "signal",
        "bg_running": 0,
        "bg_interrupted": 2,
        "bg_interrupted_ids": ["t-dead-1", "t-dead-2"],
        "tool_calls": 6,
        "elapsed_s": 420,
    }


def _skip_if_unavailable(port: int) -> None:
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        pytest.skip("playwright not installed (uv sync --group test && playwright install chromium)")
    if _port_open(port):
        pytest.skip(f"port {port} already in use")


def _open_session_and_wait_card(page, port: int, payload: dict) -> None:
    """重启后：打开会话（socket 就绪）→ 等真实广播帧；竞态 missed 则补投。"""
    page.reload(wait_until="load")
    # 侧栏点开会话行 → session socket 连接（全局事件打到每个活 socket）
    row = page.locator(".nav-item.group", has_text=USER_MSG).first
    row.wait_for(timeout=15_000)
    row.click()
    deadline = time.time() + 6
    while time.time() < deadline:
        if page.evaluate("window.__liveSockets()") >= 1:
            break
        page.wait_for_timeout(200)
    # 真实广播在 lifespan +2s；给 4s 窗口等真帧（reload 后 socket 可能赶上）
    deadline = time.time() + 4
    while time.time() < deadline:
        if page.evaluate("window.__lwReal") is not None:
            break
        page.wait_for_timeout(200)
    if page.evaluate("window.__lwReal") is None:
        page.evaluate("(p) => window.__injectLw(p)", payload)


def _card(page):
    """恢复决策卡：含请求摘要的那张 toast（role=status）。"""
    return page.locator('[role="status"]', has_text=HEAD_TEXT).first


def _wait_card(page, timeout_ms: int = 10_000):
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if _card(page).count() >= 1:
            return _card(page)
        page.wait_for_timeout(200)
    raise AssertionError("lastwords 恢复决策卡未出现")


def _run_decision_flow(tmp_path, port: int, decision: str) -> None:
    """decision: 'resume' | 'dismiss'。两条链路共享同一套重启编排。"""
    _skip_if_unavailable(port)
    from playwright.sync_api import sync_playwright

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    # ── 阶段一：起 sidecar #1，造一个真实会话 ──────────────────────────────
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), _env(home, port))
    sid = None
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.add_init_script(INIT_JS)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1500)
            ta = page.locator("textarea").first
            ta.click()
            ta.fill(USER_MSG)
            ta.press("Enter")
            page.wait_for_timeout(2000)
            sessions = _api(port, "/api/sessions")
            assert isinstance(sessions, list) and len(sessions) >= 1
            sid = str(sessions[0].get("session_id") or sessions[0].get("id"))
            assert sid
            browser.close()
    finally:
        sc.stop_sidecar(proc)

    # ── 阶段二：落盘遗言 + interrupted 任务，重启 sidecar #2 ───────────────
    payload = _seed_lastwords(home, sid)
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), _env(home, port))
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.add_init_script(INIT_JS)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1000)

            # 记录决策请求（真 fetch 的 body 是验收核心）
            reqs: list[dict] = []
            console_errs: list[str] = []
            page.on("console", lambda m: console_errs.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errs.append(str(e)))
            page.on(
                "request",
                lambda r: reqs.append({"url": r.url, "body": r.post_data})
                if r.url.endswith("/api/lastwords/resume") and r.method == "POST"
                else None,
            )

            _open_session_and_wait_card(page, port, payload)
            card = _wait_card(page)

            # ① 文案：真实请求摘要 + 非零 interrupted 后台任务数（en 文案）
            body_text = card.inner_text()
            assert HEAD_TEXT in body_text
            assert "2 background tasks killed" in body_text, body_text
            # 两个决策按钮都在
            assert card.get_by_role("button", name="Resume", exact=True).count() == 1
            assert card.get_by_role("button", name="Discard", exact=True).count() == 1

            # ② 常驻：7s（> 默认 6s 自动消失窗）后仍在
            page.wait_for_timeout(7000)
            assert _card(page).count() >= 1, "恢复决策卡必须常驻（duration:0），不许自动消失"

            # ③ 决策：真调端点
            if decision == "resume":
                card.get_by_role("button", name="Resume", exact=True).click()
            else:
                card.get_by_role("button", name="Discard", exact=True).click()

            deadline = time.time() + 15
            while time.time() < deadline and not reqs:
                page.wait_for_timeout(200)
            assert len(reqs) == 1, f"应恰好一次 /api/lastwords/resume 调用：{reqs}"
            sent = json.loads(reqs[0]["body"])
            if decision == "resume":
                assert sent == {"session_id": sid}, sent
                # 成功反馈（resume-ok 或后端 restored 回执）任一出现即可
                deadline = time.time() + 20
                ok = False
                while time.time() < deadline:
                    txt = page.locator('[role="status"]').all_inner_texts()
                    if any(
                        "Resume requested" in t or "Restart restore" in t for t in txt
                    ):
                        ok = True
                        break
                    page.wait_for_timeout(300)
                assert ok, "恢复成功反馈未出现（resume-ok / lastwords_restored 回执）"
            else:
                assert sent == {"session_id": sid, "dismiss": True}, sent
                deadline = time.time() + 10
                ok = False
                while time.time() < deadline:
                    txt = page.locator('[role="status"], [role="alert"]').all_inner_texts()
                    if any("Recovery dismissed" in t for t in txt):
                        ok = True
                        break
                    page.wait_for_timeout(300)
                assert ok, f"忽略确认反馈未出现 texts={txt} console={console_errs[-5:]}"

            # ④ 后端状态迁移：条目已消费（再 resume → no_pending）+ 文件已删
            again = _api(port, "/api/lastwords/resume", {"session_id": sid})
            assert again == {"ok": False, "error": "no_pending"}, again
            assert not (home / "lastwords" / f"{sid}.json").exists(), (
                "决策后遗言文件必须被删除（exactly-once）"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_packaged_ui_lastwords_resume_button_runs_real_resume(tmp_path):
    _run_decision_flow(tmp_path, PORT_RESUME, "resume")


def test_packaged_ui_lastwords_dismiss_button_sends_dismiss_true(tmp_path):
    _run_decision_flow(tmp_path, PORT_DISMISS, "dismiss")
