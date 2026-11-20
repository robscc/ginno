"""E2E：起真 server 子进程验证启动任务表（批次 7 lifespan 表化）。

``python -m ginno_runtime``（与桌面壳 spawn 的入口一致）在隔离
GINNO_HOME + 随机端口下完整跑一遍 36 个 startup 任务 → 轮询
``/api/health`` 直至 ok → SIGTERM 优雅退出（lifespan finally 的
10 个 shutdown 任务 + uvicorn timeout_graceful_shutdown=3s）。

断言口径（2026-11-20 对照 origin/main @bef0a51 基线实测）：
``-m`` 源码模式下 uvicorn 优雅关闭完整走完（日志含
``Application shutdown complete`` / ``Finished server process``）后，
进程仍以 signal-death（rc=-15）形态退出——main 分支即如此，属既有
行为、非表化回归。故本测试的硬断言是 **shutdown 日志出现**（shutdown
表卡死/炸掉时既不会出现该行、也不会按时退出），退出码仅要求
按时终止（0 或 -15）。
"""

from __future__ import annotations

import json as _json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pytestmark = pytest.mark.e2e

# 首次 import ginno_runtime（LangGraph 全家桶）+ 36 任务冷启动预算。
_STARTUP_TIMEOUT_S = 90.0
# SIGTERM 后：lifespan shutdown 表 + uvicorn timeout_graceful_shutdown=3s。
_TERM_TIMEOUT_S = 30.0


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=2.0
        ) as r:
            return _json.loads(r.read())
    except OSError:
        return None


def test_real_server_starts_and_shuts_down_cleanly(tmp_path):
    home = tmp_path / "ginno-home"
    port = _free_port()
    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_RUNTIME_PORT=str(port),
    )
    env.pop("GINNO_SHELL_PID", None)  # 壳看守只认桌面壳注入的 pid
    proc = subprocess.Popen(
        [sys.executable, "-m", "ginno_runtime"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    out = ""
    try:
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        health = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server exited early rc={proc.returncode}\n{out}")
            health = _get_health(port)
            if health and health.get("ok"):
                break
            time.sleep(0.3)
        else:
            proc.kill()
            pytest.fail("server did not become healthy in time")
        assert health is not None and health["ok"] is True
        assert health.get("version"), "health must self-report a version"

        # SIGTERM → 优雅退出窗口。
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=_TERM_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10.0)
            pytest.fail("server ignored SIGTERM (shutdown table stuck?)")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10.0)
        if proc.stdout:
            out = proc.stdout.read()
    # main 基线：优雅走完后仍以 -15 signal-death 收尾（见模块 docstring）。
    assert proc.returncode in (0, -signal.SIGTERM), (
        f"unexpected exit code {proc.returncode}"
    )
    # 硬证据：lifespan shutdown 表完整执行（uvicorn 收尾日志）。
    assert "Application shutdown complete" in out, (
        f"graceful shutdown log missing; got rc={proc.returncode}\n{out[-2000:]}"
    )
