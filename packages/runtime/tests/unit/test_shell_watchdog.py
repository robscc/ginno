"""server._watch_shell_pid（反孤儿化壳 pid 看守，2026-11-04）单测。

fire 注入计数器，不真发 SIGTERM：① env 缺失（裸 dev 服务）不动作直接返回；
② 壳 pid 消失 → 恰好触发一次自退；③ 壳活着 → 持续等待不触发。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from ginno_runtime import server

pytestmark = pytest.mark.unit


async def test_no_env_noop(monkeypatch):
    monkeypatch.delenv("GINNO_SHELL_PID", raising=False)
    fired: list[int] = []
    await server._watch_shell_pid(poll_s=0.01, fire=lambda: fired.append(1))
    assert not fired


async def test_dead_shell_pid_fires_once(monkeypatch):
    # 99999999：超出 macOS pid 范围的必死 pid（kill -0 → ESRCH）
    monkeypatch.setenv("GINNO_SHELL_PID", "99999999")
    fired: list[int] = []
    await server._watch_shell_pid(poll_s=0.01, fire=lambda: fired.append(1))
    assert fired == [1]


async def test_alive_shell_pid_keeps_waiting(monkeypatch):
    # 壳=本测试进程的父（pytest runner，活着）→ 不该触发
    monkeypatch.setenv("GINNO_SHELL_PID", str(os.getppid()))
    fired: list[int] = []
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            server._watch_shell_pid(poll_s=0.05, fire=lambda: fired.append(1)),
            timeout=0.3,
        )
    assert not fired
