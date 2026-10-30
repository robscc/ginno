"""Packaged-sidecar 生命周期工具（e2e 共用）。

背景（2026-10-29 清理 45 个孤儿 dist sidecar 的教训）：PyInstaller 打包的
sidecar 会忽略 SIGTERM，``finally: proc.terminate()`` 只对快退出者有效，
测试崩溃/超时路径下泄漏的进程全部变成 launchd 孤儿，积累后占内存还可能
持锁。本模块提供两层防线：

1. :func:`spawn_sidecar` 独立进程组启动 + :func:`stop_sidecar`
   TERM→等→KILL 进程组的强拆（子进程一并带走）；
2. :func:`dist_sidecar_pids` / :func:`harvest` 给会话级收割器用——只杀
   本测试会话期间新出现的 dist sidecar（快照 diff），绝不碰用户自己的。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

# PyInstaller bootloader 对 TERM 的处理不可靠（实测 45 个泄漏进程全部
# 无视 SIGTERM），等待窗口给正常退出留个机会即可，超时直接 KILL。
_TERM_GRACE_S = 5.0

_DIST_BIN_SUFFIX = "dist/ginno-runtime/ginno-runtime"


def dist_sidecar_pids() -> list[int]:
    """当前所有 dist/ginno-runtime 进程的 pid（按可执行路径匹配）。"""
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    ).stdout
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if _DIST_BIN_SUFFIX not in line:
            continue
        pid_str = line.split(maxsplit=1)[0]
        if pid_str.isdigit():
            pids.append(int(pid_str))
    return pids


def spawn_sidecar(bin_path: str, env: dict) -> subprocess.Popen:
    """独立进程组启动 packaged sidecar（``setsid`` 语义）。

    进程组是 stop_sidecar 能连子进程一起强拆的前提；Popen 默认同组，
    killpg 会误伤 pytest 自己。
    """
    return subprocess.Popen(
        [str(bin_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        # 组已消失（或无权限）——进程自身补一枪，容忍同样的失败
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def stop_sidecar(proc: subprocess.Popen) -> None:
    """TERM 进程组 → 宽限等待 → KILL 进程组（幂等，进程已死则无操作）。"""
    if proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    deadline = time.time() + _TERM_GRACE_S
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.2)
    if proc.poll() is None:
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def harvest(pids: list[int]) -> int:
    """强制 KILL 给定 pid（会话收割兜底），返回实际还活着的数量。"""
    alive = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            alive += 1
        except (ProcessLookupError, PermissionError):
            pass
    return alive
