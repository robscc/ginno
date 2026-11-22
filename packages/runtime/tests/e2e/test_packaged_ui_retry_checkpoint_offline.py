"""Packaged-UI e2e: 断线窗内 retryFromCheckpoint 不删卡、反馈可见（R3 Q3）。

修复前：错误卡上点「Resume from checkpoint」时，删卡先于 socket 检查——
断线窗口内点击会静默把卡删掉、什么都不发生（内容与重试入口一起消失）。

流程（真实 sidecar + 真实失败 turn，无 mock）：
1. provider 指向死端口（127.0.0.1:1，不开 fake LLM）→ 发消息 → 模型调用
   失败 → 真实 turn.error → ErrorCard 落地（带 retry/resume 按钮）；
2. context.set_offline(True) 掐断 WebSocket（断线窗）；
3. 点「Resume from checkpoint」；
4. 断言：卡保留（data-testid=error-card 仍在）+ 可见等待提示 toast
   （net.resumeWaiting，en catalog）——不再静默。

60s 超时恢复路径由 vitest（actions.test.ts，fake timers）覆盖，e2e 不等。
复用 tasks_overview 的 source/packaged harness（随机端口 + 临时 GINNO_HOME，
不碰 8787/真实设置）。
"""

from __future__ import annotations

import json
import os
import time
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest
import test_packaged_ui_tasks_overview as smoke
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

_REPO = Path(__file__).resolve().parents[2]  # packages/runtime

WAITING_SNIPPET = "waiting to reconnect"


def _auth_fail_server():
    """本地 401 server：模型调用拿到不可重试的认证错误 → turn 立即失败。

    （连死端口会进 rate_limited_backoff 的 30s×5 退避，e2e 等不起；
    401 认证错误不重试，失败即刻。）"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"bad key","type":"authentication_error"}}')

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture
def app(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    # 不设 GINNO_FAKE_LLM：custom provider 指向本地 401 server，
    # 模型调用失败 → 真实 error 卡。
    _srv, llm_port = _auth_fail_server()
    port = smoke._free_port()
    settings = smoke._settings()
    settings["providers"]["custom"]["base_url"] = f"http://127.0.0.1:{llm_port}/v1"
    (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    env = dict(os.environ, GINNO_HOME=str(tmp_path), GINNO_RUNTIME_PORT=str(port))
    env.pop("GINNO_FAKE_LLM", None)
    env.pop("GINNO_FAKE_LLM_SCRIPTS", None)
    err_log = (tmp_path / "sidecar-stderr.log").open("wb")
    if smoke.RUNTIME_MODE == "source":
        proc = subprocess.Popen(
            ["uv", "run", "--no-sync", "ginno-runtime"],
            cwd=str(_REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=err_log,
            start_new_session=True,
        )
    else:
        proc = smoke.sc.spawn_sidecar(str(smoke.RUNTIME_BIN), env, err=err_log)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                yield page, port
            finally:
                browser.close()
    finally:
        smoke._stop(proc, err_log)


def _error_card(page):
    return page.locator('[data-testid="error-card"]')


def _toasts(page):
    # NoticeHost 卡片：error 级 role=alert，其余（含 warn）role=status；
    # 都排除 Next.js 常驻的 sr-only 路由播报器（也是 role=alert，空文本）。
    return page.locator('[role="alert"]:not(#__next-route-announcer__), [role="status"]:not(#__next-route-announcer__)')


def test_retry_from_checkpoint_offline_keeps_card_and_warns(app):
    page, port = app
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=20_000)
    ta.click()
    ta.fill("please fail")
    ta.press("Enter")

    # 模型调用失败（auto_retry 两轮后）→ 真实 error 卡 + resume 按钮可用
    card = _error_card(page).first
    card.wait_for(state="visible", timeout=60_000)
    resume_title = "Continue from the latest checkpoint, keeping completed tool calls and intermediate results"
    resume = card.get_by_title(resume_title)
    resume.wait_for(state="visible", timeout=30_000)
    expect(resume).to_be_enabled(timeout=60_000)

    # 断线窗：掐断此后所有 WS 连接并刷新页面——前端重连全部失败，处于
    # 断线态；error 卡从服务端 history 恢复（last_error 持久化路径）。
    # 断线注入（add_init_script 覆写页面 WebSocket）：构造后立即 close，
    # readyState 永远到不了 OPEN——「runtime 不可达」语义确定成立，前端
    # 3s 重连循环每轮都同样瞬断。不用 route_web_socket：sync API 里
    # WebSocketRoute.close/connect 组合会卡 dispatch fiber 或不向页面侧
    # 回传 close（页面残留 OPEN，点击走正常发送路径）；page.route 也拦不了
    # ws:// 握手。只在 reload 前注入，对后续导航生效。
    page.add_init_script(
        """
        (() => {
          const RealWS = window.WebSocket;
          // 「runtime 不可达」测试替身：readyState 恒 CLOSED、send 抛错。
          // （只 close() 不够——对真实 sidecar 的握手仍会短暂完成，重连
          // 循环里每轮都有 OPEN 瞬窗，点击落在窗口内就走了正常发送路径。）
          window.WebSocket = class extends RealWS {
            get readyState() { return RealWS.CLOSED; }
            send() { throw new Error("WebSocket is closed (test stub)"); }
          };
        })();
        """
    )
    try:
        page.reload(wait_until="load")
        # 懒建会话不进 URL：从侧栏重进会话，history 恢复错误卡
        page.locator("text=please fail").first.wait_for(timeout=20_000)
        page.locator("text=please fail").first.click()
        card2 = _error_card(page).first
        card2.wait_for(state="visible", timeout=20_000)
        resume2 = card2.get_by_title(resume_title)
        resume2.wait_for(state="visible", timeout=20_000)
        # WS 早已瞬断（reload 即注入）——等重连循环至少转过一轮再点击，
        # 确保 socketsRef 里是 CLOSED socket 而非 CONNECTING 初值。
        page.wait_for_timeout(1_000)
        resume2.click()

        # 卡保留 + 可见等待提示（修复前：卡被静默删除、无任何反馈）。
        # 注意 toast 6s 自动消失且进出场带 opacity 过渡（中途会有不可见帧），
        # 断言打在「文本已渲染」而非 visible 态，避免竞态。
        expect(_toasts(page)).to_contain_text(WAITING_SNIPPET, timeout=10_000)
        assert _error_card(page).count() >= 1
    finally:
        pass
