"""入站 A2A shell 的 OS 级沙箱（bubblewrap）。

## 为什么需要

`guard.check_tool` 的 exec_root 边界对**文件工具**生效，但对 **shell 不生效**：
`_guard_kwargs` 给 bash 设 `target_path=exec_root`（根本身）使边界检查恒过，
而命令串只在 `trust=readonly` 时查白名单。于是 trust=normal/full 的远端派活
可以用 `echo x >> /etc/hosts`、`cat <任意可读文件>`、`curl … | sh` 逃逸。
**exec_root 不是 shell 的安全边界，真正的边界只有 OS 用户权限。**

命令级路径解析不可靠（shell 图灵完备：变量/eval/base64/符号链接都能绕），
给的是虚假安全感。这里用 **user namespace 沙箱**做真隔离：沙箱内根本看不到
exec_root 之外的文件系统。

## 为什么选 bubblewrap 而不是容器

- 无 daemon、无常驻内存，单次调用开销毫秒级 —— 1GB 内存的 VPS 可承受
- 直接用内核 user namespace，不需要 root
- 实测（Ubuntu 22.04 / 5.15 / ginno 非 root 用户）：`/etc/shadow`、`/etc/hosts`、
  密钥文件全部 **No such file**，而 exec_root 可写

## 边界与开关

- **只对入站 A2A 生效**：由 worker 设 `GINNO_A2A_SANDBOX_ROOT`。桌面用户自己的
  bash 完全不受影响（不能因为远端安全需求把本机工作流也关进沙箱）。
- **fail-closed**：要求沙箱但 bwrap 不可用/不支持 → **拒绝执行**并说明原因，
  绝不静默降级为无沙箱（那等于洞还在却以为修好了）。
- 网络默认**保留**（`--unshare-all` 后显式 `--share-net`）：节点要能出网干活，
  这是用户明确要求的档位；文件系统隔离才是本模块的目标。
"""

from __future__ import annotations

import os
import shutil
import subprocess

ENV_ROOT = "GINNO_A2A_SANDBOX_ROOT"
ENV_DISABLE = "GINNO_A2A_SANDBOX_DISABLE"

# 只读挂进沙箱的系统目录：够跑常见命令，又不暴露 /etc、/home、/root、/var。
_RO_BINDS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt")


class SandboxUnavailable(RuntimeError):
    """要求沙箱但无法提供。fail-closed：调用方必须拒绝执行。"""


def sandbox_root() -> str:
    """当前是否处于「必须沙箱」的入站上下文；返回 exec_root 或空串。"""
    if os.environ.get(ENV_DISABLE):
        return ""
    return str(os.environ.get(ENV_ROOT) or "").strip()


def bwrap_path() -> str:
    return shutil.which("bwrap") or ""


def _probe(bwrap: str) -> bool:
    """探一次 bwrap 是否真能用（有些内核/容器禁 user namespace）。"""
    try:
        r = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "/bin/true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wrap_argv(
    argv: list[str], cwd: str | None, extra_ro: list[str] | None = None
) -> list[str]:
    """把 shell argv 包进 bwrap。非入站上下文原样返回。

    要求沙箱但不可用 → 抛 SandboxUnavailable（fail-closed，绝不静默降级）。
    沙箱内工作目录固定 /work，绑定到 exec_root；宿主 cwd 保持不变。

    ``extra_ro``：额外只读绑入的路径（如解释器与执行器脚本所在目录）。
    repl 的执行器是 ``sys.executable <executor.py>``，两者未必落在
    _RO_BINDS 覆盖的目录里，不显式绑入沙箱内就找不到 → 必须传。
    """
    root = sandbox_root()
    if not root:
        return list(argv)
    if not os.path.isdir(root):
        raise SandboxUnavailable(f"沙箱根目录不存在: {root!r}")
    bwrap = bwrap_path()
    if not bwrap:
        raise SandboxUnavailable(
            "入站 shell 需要 bubblewrap 沙箱，但本机未安装 bwrap。"
            "请安装 bubblewrap（Debian/Ubuntu: apt-get install bubblewrap），"
            "或显式关闭入站 shell 能力。绝不静默以无沙箱方式执行。"
        )
    if not _probe(bwrap):
        raise SandboxUnavailable(
            "bwrap 存在但无法创建 user namespace（内核/容器限制）。"
            "入站 shell 拒绝执行——绝不静默降级为无沙箱。"
        )
    out = [bwrap]
    for d in _RO_BINDS:
        if os.path.isdir(d):
            out += ["--ro-bind", d, d]
    # DNS（2026-11-18 修）：最小 /etc 把 resolv.conf/hosts 一起丢了 →
    # 沙盒内 curl/git/pip 全部 CURLE_COULDNT_RESOLVE_HOST（exit 6），
    # 而宿主网络与路由完全正常（真机实证：直接 curl 1.1.1.1 成功）。
    # 只读绑这两个**文件**：内容仅 nameserver IP/主机名映射，无敏感信息；
    # /etc 其余路径（含 /etc/ginno-node/env、/etc/shadow）依旧不可见，
    # 写入依旧被 ro-bind 拒绝（原有逃逸回归不受影响）。宿主没有该文件
    # 则跳过（不因缺失而 fail）。
    for dns_file in ("/etc/resolv.conf", "/etc/hosts"):
        if os.path.isfile(dns_file):
            out += ["--ro-bind", dns_file, dns_file]
    # 额外只读路径（解释器/执行器）：去重且跳过已被 _RO_BINDS 覆盖的
    for d in extra_ro or []:
        d = str(d)
        if not d or not os.path.exists(d):
            continue
        if any(d == b or d.startswith(b.rstrip("/") + "/") for b in _RO_BINDS):
            continue
        out += ["--ro-bind", d, d]
    out += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", root, "/work",
        "--chdir", "/work",
        "--unshare-all",
        "--share-net",  # 节点要能出网干活；隔离目标是文件系统
        "--die-with-parent",
        "--new-session",
    ]
    out += list(argv)
    return out


def sandbox_cwd(cwd: str | None) -> str | None:
    """宿主侧 Popen 的 cwd。

    ⚠️ **绝不能返回 "/work"**：Popen 的 cwd 在**宿主**上生效（exec bwrap 之前），
    而 /work 只存在于沙箱内 → 会 FileNotFoundError。沙箱内的工作目录由
    bwrap 的 `--chdir /work` 负责，宿主侧保持原 cwd（或 None）即可。
    """
    return cwd


def describe() -> str:
    """给日志/审计用的一行状态描述。"""
    root = sandbox_root()
    if not root:
        return "sandbox=off (not an inbound A2A context)"
    bw = bwrap_path()
    return f"sandbox=bwrap root={root} bwrap={bw or 'MISSING'}"
