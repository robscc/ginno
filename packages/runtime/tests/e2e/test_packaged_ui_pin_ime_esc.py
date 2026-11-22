"""Packaged-UI e2e：pin 窗 IME 合成中 Esc 不收窗（Q8，2026-11）。

现象：中文输入法在 pin 窗输入时按 Esc 取消候选词，pin 窗被误收成 pill
（window 级 keydown 缺 isComposing 守卫）。

验证深度（如实）：Chromium 无法真正激活系统 IME（CDP 也只有
insertText，无组合候选态），本用例用页面内合成的 ``KeyboardEvent`` 且
``isComposing=true`` 派发到 window——覆盖的是「渲染层监听器对合成态
keydown 的响应」这一真实路径（事件穿过 PinApp 的 window listener、
React 状态、DOM 重渲），比 vitest 纯函数断言高一档；keyCode=229 兜底
分支（旧引擎）由 vitest ``pinShortcuts.test.ts`` 覆盖。

判据（确定性，无视觉回弹依赖）：
* mini 态 → ``[data-testid=pin-mini-root]`` 可见；
* 合成 Esc 后仍 mini、pill 根不出现；
* 非合成 Esc 后 → ``[data-testid=pin-pill-root]`` 可见（原行为不回归）。

运行模式（GINNO_E2E_RUNTIME）：``packaged``（默认，dist bundle）/
``source``（源码 sidecar + repo 内 apps/web/out 静态导出）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_REPO = Path(__file__).resolve().parents[2]  # packages/runtime
_RUNTIME_DIST = _REPO / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime"
WEB_OUT = _REPO.parents[1] / "apps" / "web" / "out"

RUNTIME_MODE = os.environ.get("GINNO_E2E_RUNTIME", "packaged").strip().lower()
STRICT = os.environ.get("GINNO_E2E_STRICT") == "1"


def _skip_or_fail(msg: str):
    if STRICT:
        pytest.fail(f"[GINNO_E2E_STRICT] {msg}")
    pytest.skip(msg)


def _require_runtime():
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        _skip_or_fail("playwright not installed")
    if RUNTIME_MODE == "source":
        if not (_REPO / "src" / "ginno_runtime" / "server.py").exists():
            _skip_or_fail("source mode needs the repo runtime sources")
        if not (WEB_OUT / "pin.html").exists():
            _skip_or_fail("source mode needs the web static export (pnpm build in apps/web)")
        if not shutil.which("uv"):
            _skip_or_fail("source mode needs uv on PATH")
        return
    if not RUNTIME_BIN.is_file():
        _skip_or_fail("packaged sidecar not built (run `make runtime`)")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"sidecar on :{port} did not become healthy in {timeout}s")


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


def _spawn(tmp_path: Path, port: int):
    (tmp_path / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")
    env = dict(os.environ, GINNO_HOME=str(tmp_path), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(port))
    err_log = (tmp_path / "sidecar-stderr.log").open("wb")
    if RUNTIME_MODE == "source":
        proc = subprocess.Popen(
            ["uv", "run", "--no-sync", "ginno-runtime"],
            cwd=str(_REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=err_log,
            start_new_session=True,
        )
        return proc, err_log
    return sc.spawn_sidecar(str(RUNTIME_BIN), env, err=err_log), err_log


def _dispatch_esc(page, composing: bool):
    """页面内派发 keydown 到 window（合成态可设 isComposing；键盘焦点
    无关——PinApp 的监听器挂在 window 上，这正是被修复的路径）。"""
    page.evaluate(
        """(composing) => {
          window.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'Escape', bubbles: true, cancelable: true, isComposing: composing,
          }));
        }""",
        composing,
    )


def test_pin_ime_esc_keeps_mini_and_plain_esc_collapses(tmp_path):
    _require_runtime()
    port = _free_port()
    proc, err_log = _spawn(tmp_path, port)
    try:
        _wait_health(port)
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(f"http://127.0.0.1:{port}/pin")
                mini = page.locator('[data-testid="pin-mini-root"]')
                pill = page.locator('[data-testid="pin-pill-root"]')
                mini.wait_for(state="visible", timeout=20_000)

                # IME 合成中的 Esc：取消候选，pin 窗必须留在 mini。
                _dispatch_esc(page, composing=True)
                page.wait_for_timeout(300)  # 让可能的错误重渲有机会发生
                assert mini.is_visible(), "IME composing Esc collapsed the pin window (regression)"
                assert pill.count() == 0 or not pill.is_visible(), (
                    "pill root appeared after composing Esc"
                )

                # 非合成 Esc：原行为——收成 pill。
                _dispatch_esc(page, composing=False)
                pill.wait_for(state="visible", timeout=5_000)
                assert not mini.is_visible(), "mini root still visible after plain Esc"
            finally:
                browser.close()
    finally:
        try:
            sc.stop_sidecar(proc)
        finally:
            err_log.close()
