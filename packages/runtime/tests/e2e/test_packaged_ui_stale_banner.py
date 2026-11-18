"""Packaged-UI e2e：过时失败恢复横幅在新一轮对话后自动消失（2026-11-18）。

CLAUDE.md 红线（2026-11-12 起）：前端交互改动必须配 packaged UI 用例——
vitest/tsc 抓不到渲染层断裂。本文件钉住浏览器级终态：

- 现象：某 turn 失败后出现红色恢复横幅（「请求失败」+ 从头重试/从
  checkpoint 续跑）。用户随后开了新一轮对话，旧横幅仍悬在新对话上方——
  过时的恢复入口点了会重放旧 turn。
- 修复：渲染层判定 failedTurnBannerVisible——横幅只在失败 turn 仍是会话
  最后一个 turn 时渲染；出现更新的用户/助手消息即整体隐藏。

场景编排（goal 边界②，同时覆盖核心回归）：provider 指向本地 401 server
（AuthenticationError → 4xx 不 auto-retry → turn 秒级失败）——
  1. 发第一条消息 → turn 失败 → 恰好 1 张 error 卡（回归：末位失败横幅在）；
  2. 发第二条消息 → 又一个失败 turn → **仍恰好 1 张**（不是 2 张：
     旧的消失、新的出现——若未修复，旧横幅残留计数为 2）。

为什么不直接学 llm_events 把 base_url 指 loopback:1（connection refused）：
connection 类错误**可重试**（graph_models.is_retryable_error），auto-retry
退避 5s+20s（AUTO_RETRY_BACKOFF_S 固定常量）后 turn 才 emit error——每个
失败 turn 要等 ~30s，两条消息的用例奔 90s+；401 是确定性 4xx，不重试、
秒级收口，同样的失败终态、稳定得多。

「新 turn 成功后旧横幅消失」（用户截图原始场景）由 vitest 纯函数用例
streamCoreFailedBanner.test.ts 覆盖——fake/real provider 是 sidecar 启动期
env（GINNO_FAKE_LLM），同一进程内无法先失败后成功，浏览器级构造该场景
需中途换 provider（构造成本高于收益，PR body 有说明）。

与 test_packaged_ui_llm_events.py 同基建：dist sidecar + 真实 Chromium，
GINNO_HOME 隔离 tmp_path，bind(0) 随机空闲口（不碰 8787），语言钉 zh-CN。
Skips gracefully when the binary isn't built or playwright isn't installed.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST

# zh-CN 断言词（messages/zh-CN/chat.json）
RETRY_LABEL = "从头重试"        # errorCard.retry


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


class _AuthFailHandler(BaseHTTPRequestHandler):
    """任何路径都回 401——openai SDK 抛 AuthenticationError（确定性 4xx，
    is_retryable_error=False，不进 auto-retry 退避），turn 秒级失败。"""

    def do_POST(self) -> None:  # noqa: N802
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"error": {"message": "bad key (e2e)", "type": "invalid_request_error"}}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.do_POST()

    def log_message(self, *args) -> None:  # 静音访问日志
        pass


@contextmanager
def _auth_fail_server():
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), _AuthFailHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        srv.shutdown()
        srv.server_close()


def _settings(base_url: str) -> dict:
    # language 钉 zh-CN；custom provider 指向本地 401 server——不开 fake LLM，
    # turn 必然 AuthenticationError（确定性失败态）。
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
                       "api_key": "k", "base_url": base_url, "model": "m",
                       "default_model": "m", "max_tokens": 100, "temperature": 0.7,
                       "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _sessions(port: int) -> list:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/sessions?project_slug=default", timeout=5
    ) as r:
        return json.loads(r.read().decode())


def _wait_turn_error(page, port: int, not_turn: str | None = None, timeout: float = 30) -> dict:
    """轮询等服务端 last_error 落定（turn 失败的权威收口信号）。

    不看 sessions 的 running 生命窗：401 turn 从起跑到失败 ~100ms，300ms
    轮询间隔会整个滑过；last_error.at/turn_id 由错误收口路径写入
    （engine.py `_session_meta_patch`），每次失败必更新。not_turn 传入
    上一轮的 turn_id 即可等「新一轮的失败」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rows = _sessions(port)
            le = rows[0].get("last_error") if rows else None
            if le and (not_turn is None or le.get("turn_id") != not_turn):
                return le
        except Exception:
            pass
        page.wait_for_timeout(300)
    raise AssertionError("turn 失败（last_error）未在超时内落定")


@contextmanager
def _boot(tmp_path, base_url: str):
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
    (home / "settings.json").write_text(json.dumps(_settings(base_url)), encoding="utf-8")
    env = dict(os.environ, GINNO_HOME=str(home), GINNO_RUNTIME_PORT=str(port))
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


def _send_message(page, text: str) -> None:
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=30_000)
    ta.click()
    ta.fill(text)
    ta.press("Enter")


def test_stale_recovery_banner_hidden_after_new_turn(tmp_path):
    """失败 turn → 新一轮对话 → 旧横幅消失（新失败横幅恰好 1 张）。"""
    with _auth_fail_server() as base_url, _boot(tmp_path, base_url) as (page, port):
        # 第一次 turn：失败 → 横幅出现（回归：末位失败横幅必须在）
        _send_message(page, "first doomed turn")
        first_error = _wait_turn_error(page, port)
        page.wait_for_timeout(1500)  # 渲染缓冲（llm_events 同款）
        cards = page.get_by_test_id("error-card")
        assert cards.count() == 1, (
            f"第一个失败 turn 后应恰好 1 张恢复横幅，实际 {cards.count()}"
        )
        assert page.get_by_text(RETRY_LABEL).count() >= 1, "横幅应带「从头重试」CTA"

        # 第二次 turn：又失败 → 新横幅出现、旧的消失——仍恰好 1 张。
        # 若未修复（旧横幅残留），这里会是 2 张，断言直接红。
        _send_message(page, "second doomed turn")
        _wait_turn_error(page, port, not_turn=first_error["turn_id"])
        page.wait_for_timeout(1500)
        assert page.get_by_test_id("error-card").count() == 1, (
            "新一轮对话后旧失败横幅应消失（至多 1 张），"
            f"实际 {page.get_by_test_id('error-card').count()}"
        )
