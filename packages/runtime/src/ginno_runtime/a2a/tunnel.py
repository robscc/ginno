"""``ginno tunnel`` —— 到远端 ginno node 的 SSH 隧道守护（P1-4，2026-11-16）。

为什么需要它：node 故意只绑 127.0.0.1（`resolve_bind` 拒 wildcard，设计防呆），
外部访问一律走 SSH -L 隧道。原状是用户手敲 ssh 命令、断了再手敲——本模块把
「建隧道 + 断线自动重连 + 算出本机可粘贴的能力地址」收成一条命令。

用法::

    ginno tunnel --host 1.2.3.4 --key ~/.ssh/id_ed25519 \\
        --token Q3EQ... --remote-port 8790 --local-port 18790

打印的本机地址可直接粘进桌面 Ginno 的 A2A 连接器。

设计取向：
- **不引入新依赖**：直接调系统 ssh（ServerAliveInterval 保活），与用户既有
  SSH 配置/agent 兼容；不自造加密层。
- **不放行远端端口**：只做 -L 本地转发，远端仍只绑环回。
- **明文 http 不附指纹**：隧道本身提供加密，地址按 build_address 口径不带 #fp。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time

DEFAULT_REMOTE_PORT = 8790
DEFAULT_LOCAL_PORT = 18790
_RETRY_S = 5.0


def build_ssh_argv(
    *,
    host: str,
    user: str,
    key: str | None,
    local_port: int,
    remote_port: int,
    extra: list[str] | None = None,
) -> list[str]:
    """组 ssh -L 命令。保活 + 转发失败即退（便于外层重连）。"""
    argv = [
        "ssh",
        "-N",
        "-L",
        f"{local_port}:127.0.0.1:{remote_port}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key:
        argv += ["-i", key]
    if extra:
        argv += list(extra)
    argv.append(f"{user}@{host}")
    return argv


def local_capability_address(local_port: int, token: str) -> str:
    """本机可粘贴的能力地址（明文 http，不带 #fp——加密由隧道提供）。"""
    from .address import build_address

    return build_address(f"http://127.0.0.1:{local_port}", token, "")


def run_tunnel(
    *,
    host: str,
    user: str = "root",
    key: str | None = None,
    local_port: int = DEFAULT_LOCAL_PORT,
    remote_port: int = DEFAULT_REMOTE_PORT,
    token: str = "",
    once: bool = False,
    extra: list[str] | None = None,
) -> int:
    """建隧道并守护重连。once=True 时断线即退（供脚本/测试用）。

    返回进程退出码；Ctrl-C 干净退出 0。
    """
    if shutil.which("ssh") is None:
        print("找不到 ssh 可执行文件，请先安装 OpenSSH 客户端", file=sys.stderr)
        return 2
    argv = build_ssh_argv(
        host=host, user=user, key=key,
        local_port=local_port, remote_port=remote_port, extra=extra,
    )
    if token:
        print("本机能力地址（粘进桌面 Ginno 的 A2A 连接器）：")
        print(f"  {local_capability_address(local_port, token)}")
    print(f"隧道：127.0.0.1:{local_port} → {user}@{host}:127.0.0.1:{remote_port}")
    print("Ctrl-C 退出。断线会自动重连。")
    try:
        while True:
            proc = subprocess.run(argv, check=False)
            if once:
                return proc.returncode
            print(
                f"[tunnel] 断开（exit={proc.returncode}），{_RETRY_S:.0f}s 后重连…",
                flush=True,
            )
            time.sleep(_RETRY_S)
    except KeyboardInterrupt:
        print("\n[tunnel] 已退出")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ginno tunnel",
        description="SSH tunnel to a remote ginno node, with auto-reconnect.",
    )
    p.add_argument("--host", required=True, help="remote host (IP or name)")
    p.add_argument("--user", default="root", help="SSH user (default: root)")
    p.add_argument("--key", default=None, metavar="P", help="SSH private key path")
    p.add_argument(
        "--local-port", type=int, default=DEFAULT_LOCAL_PORT,
        help=f"local listen port (default: {DEFAULT_LOCAL_PORT})",
    )
    p.add_argument(
        "--remote-port", type=int, default=DEFAULT_REMOTE_PORT,
        help=f"remote node port (default: {DEFAULT_REMOTE_PORT})",
    )
    p.add_argument(
        "--token", default="",
        help="node capability token; given → print the local capability address",
    )
    p.add_argument(
        "--once", action="store_true",
        help="exit on disconnect instead of reconnecting (for scripts/tests)",
    )
    return p


def cli_main(argv: list[str] | None = None) -> int:
    args, extra = build_parser().parse_known_args(argv)
    return run_tunnel(
        host=args.host, user=args.user, key=args.key,
        local_port=args.local_port, remote_port=args.remote_port,
        token=args.token, once=args.once, extra=extra or None,
    )
