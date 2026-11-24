"""Packaged-sidecar 生命周期工具（e2e 共用）。

背景（2026-10-29 清理 45 个孤儿 dist sidecar 的教训）：PyInstaller 打包的
sidecar 会忽略 SIGTERM，``finally: proc.terminate()`` 只对快退出者有效，
测试崩溃/超时路径下泄漏的进程全部变成 launchd 孤儿，积累后占内存还可能
持锁。本模块提供两层防线：

1. :func:`spawn_sidecar` 独立进程组启动 + :func:`stop_sidecar`
   TERM→等→KILL 进程组的强拆（子进程一并带走）；
2. :func:`stop_spawned_sidecars` 在会话结束时清理本 pytest 进程启动的
   sidecar。每个 xdist worker 持有自己的子进程句柄。
"""

from __future__ import annotations

import os
import signal
import subprocess

# PyInstaller bootloader 对 TERM 的处理不可靠（实测 45 个泄漏进程全部
# 无视 SIGTERM），等待窗口给正常退出留个机会即可，超时直接 KILL。
_TERM_GRACE_S = 5.0

_SPAWNED: set[subprocess.Popen] = set()


def spawn_sidecar(bin_path: str, env: dict, err=None) -> subprocess.Popen:
    """独立进程组启动 packaged sidecar（``setsid`` 语义）。

    进程组是 stop_sidecar 能连子进程一起强拆的前提；Popen 默认同组，
    killpg 会误伤 pytest 自己。
    """
    proc = subprocess.Popen(
        [str(bin_path)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err if err is not None else subprocess.DEVNULL,
        start_new_session=True,
    )
    _SPAWNED.add(proc)
    return proc


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
    if proc.poll() is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=_TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return  # 留给会话清理再试
    _SPAWNED.discard(proc)


def stop_spawned_sidecars() -> None:
    """会话兜底，仅清理本进程经 spawn_sidecar 启动的子进程。"""
    for proc in tuple(_SPAWNED):
        stop_sidecar(proc)
