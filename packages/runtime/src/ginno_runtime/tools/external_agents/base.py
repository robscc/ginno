"""External-agent delegation base: interface + IR + shared helpers.

一个 CLI 一个 adapter（claude_code.py / codex.py / pi.py / dsh.py，connector-module
同款模式），全部实现这里的 ExternalBackend 接口。本文件只放接口与共用
设施：结果类型（DelegationResult，含转录 IR）、JSONL 迭代与 usage 映射、
CLI 路径解析（GUI PATH 兜底）、宽松 JSON 解析。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol

from ginno_runtime import jsonx as json

DELEGATE_TOOL_NAME = "delegate_agent"

# Delegation runs an entire agent loop — give it real headroom, but never
# let a stalled child sit for half an hour.
_TIMEOUT_MIN_S = 10
_TIMEOUT_MAX_S = 1800
_TIMEOUT_DEFAULT_S = 1800  # 2026-10-05：600→1800（用户要求）；0=永不超时

# 最小版本门（Multica MinVersions 同思想）：低于门不硬拦，但在工具结果里
# 明示「请升级」——老 CLI 的事件流 schema 漂移是解析器假成功的常见根因。
# pi 无已知下限，不设门。
_MIN_CLI_VERSIONS: dict[str, tuple[int, int, int]] = {
    "claude": (2, 0, 0),
    "codex": (0, 100, 0),
    "dsh": (0, 2, 0),
}

_DIAGNOSTIC_CAP = 2000


def _tail(text: str, cap: int = _DIAGNOSTIC_CAP) -> str:
    text = text or ""
    return text if len(text) <= cap else "…" + text[-cap:]


@dataclass
class DelegationResult:
    """One finished delegation run. Never carries an exception."""

    output: str = ""
    stop_reason: str = "success"  # success | error | timeout
    diagnostic: str = ""
    # Normalized token shape (usage.py USAGE_FIELDS) — empty when the
    # backend reports nothing.
    usage: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    # 转录 IR（external-agents-design.md §9）：assistant / tool_result 条目，
    # delegation_sessions 把它转成 LangChain 消息写入可回放的子会话。
    transcript: list = field(default_factory=list)


class ExternalBackend(Protocol):
    """A provider of external coding agents (one per CLI)."""

    name: str
    # 二进制名（与 name 可能不同：claude-code → claude）——ENOENT 自愈与
    # 版本探针按它踢缓存/探版本。
    cli_name: str

    def available(self) -> str | None:
        """Absolute path of the CLI, or None when not installed."""
        ...

    def configured(self) -> bool:
        """安装之外是否还需首次初始化(claude/codex 登录即配好;
        pi 需要终端跑一次生成 ~/.pi)。False 时设置页给引导提示。"""
        return True

    def build_argv(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        out_file: str | None,
        session_id: str | None = None,
    ) -> list[str]:
        """``session_id`` = 续跑之前某次委托的外部会话 id（可选；只有
        supports_resume=True 的后端认它，其余忽略）。"""
        ...

    # 可选钩子（不强制实现）：adapter 专属的子进程环境变量，如 dsh 的
    # DSH_PERMISSION_MODE（dsh 的权限只认环境变量，不进 argv）。run_delegation
    # 用 getattr 探测并合并 os.environ；未实现的 backend 继承父进程环境，
    # 行为与引入该钩子前完全一致。
    # def build_env(self, mode: str, cwd: str) -> dict[str, str]: ...

    # 可选类属性：是否支持带 session_id 续跑之前的委托（目前仅 dsh=True）。
    # 工具层/机器头用 getattr(be, "supports_resume", False) 探测。
    # supports_resume: bool = False

    def parse_result(
        self, stdout: str, stderr: str, rc: int, out_file: str | None
    ) -> DelegationResult:
        ...


# --------------------------------------------------------------------------- #
# CLI resolution
# --------------------------------------------------------------------------- #


_CLI_CACHE: dict[str, str | None] = {}


def _resolve_cli(name: str) -> str | None:
    """Locate a CLI on PATH, falling back to the user's login shell.

    Ginno launched from Finder inherits a bare GUI environment (the bash
    tool uses ``$SHELL -lc`` for the same reason — see builtin.py), so a
    plain ``shutil.which`` misses homebrew/~/.local/bin entries exported in
    zshrc. Resolved paths are cached per process.
    """
    if name in _CLI_CACHE:
        return _CLI_CACHE[name]
    found = shutil.which(name)
    if not found:
        # 登录 shell 兜底有盲区:zsh -lc 只加载 .zprofile,不读 .zshrc,
        # 而很多用户(如本机)只在 .zshrc 里 export homebrew PATH。这里把
        # 常见安装目录显式补进 PATH 再 which 一次(2026-10-06:GUI 重启后
        # 三后端全部误报 not installed 即此盲区)。
        home = os.path.expanduser("~")
        extra = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/sbin"]
        for sub in (".local/bin", "bin"):
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                extra.append(p)
        found = shutil.which(
            name, path=os.pathsep.join([os.environ.get("PATH", ""), *extra]))
    if not found:
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            r = subprocess.run(
                [shell, "-lc", f"command -v {name}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln]
                found = lines[0] if lines else None
        except Exception:  # noqa: BLE001 — resolution must never raise
            found = None
    found = found or None
    _CLI_CACHE[name] = found
    return found


def clear_cli_cache() -> None:
    """Drop the resolution cache (tests switch PATH between cases)."""
    _CLI_CACHE.clear()
    _CLI_VERSION_CACHE.clear()


def invalidate_cli(name: str) -> None:
    """Drop ONE cli's cached path (and version) — ENOENT self-heal: a pinned
    path can vanish mid-run (nvm in-place upgrade); the next resolve() then
    re-walks PATH / login shell instead of failing forever until restart."""
    _CLI_CACHE.pop(name, None)
    _CLI_VERSION_CACHE.pop(name, None)


# --------------------------------------------------------------------------- #
# CLI version probe + minimum-version gate
# --------------------------------------------------------------------------- #

# path → (path, parsed tuple | None)：路径没变就复用解析结果（探一次
# --version 要起一个进程，委托调用频率下不做每次现探）。
_CLI_VERSION_CACHE: dict[str, tuple[str | None, tuple[int, int, int] | None]] = {}
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def cli_version(name: str) -> tuple[int, int, int] | None:
    """``<cli> --version`` → (major, minor, patch)；解析不出返回 None。

    10s 超时；stdout/stderr 都搜（有的 CLI 版本走 stderr）。缓存键是解析
    路径——升级后路径变化自动失效，原地替换二进制则由 invalidate_cli 在
    ENOENT 时踢掉。"""
    cli = _resolve_cli(name)
    if not cli:
        return None
    cached = _CLI_VERSION_CACHE.get(name)
    if cached and cached[0] == cli:
        return cached[1]
    v: tuple[int, int, int] | None = None
    try:
        r = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=10
        )
        m = _SEMVER_RE.search((r.stdout or "") + "\n" + (r.stderr or ""))
        if m:
            v = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    except Exception:  # noqa: BLE001 — 版本探针绝不影响委托
        v = None
    _CLI_VERSION_CACHE[name] = (cli, v)
    return v


def cli_version_warning(name: str) -> str:
    """低于最小版本门时的明示文案；无门 / 版本达标 / 解析不出 → ""。"""
    minimum = _MIN_CLI_VERSIONS.get(name)
    if not minimum:
        return ""
    v = cli_version(name)
    if v is None or v >= minimum:
        return ""
    return (
        f"[warning] {name} CLI v{v[0]}.{v[1]}.{v[2]} is below the recommended "
        f"minimum v{minimum[0]}.{minimum[1]}.{minimum[2]} — upgrade the CLI; "
        "older versions may misbehave or produce unparsable output"
    )


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


def _parse_json_lenient(text: str) -> dict | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                return data if isinstance(data, dict) else None
            except Exception:  # noqa: BLE001
                return None
        return None


# --------------------------------------------------------------------------- #
# Delegation transcript IR（三个后端共用的中间表示）
# --------------------------------------------------------------------------- #
# 解析器把子代理的过程流归一成两种条目，delegation_sessions 再转成
# LangChain 消息写入可回放子会话：
#   {"role": "assistant", "text": str, "tool_calls": [{"id","name","args"}]}
#   {"role": "tool_result", "id": str, "name": str, "content": str, "is_error": bool}


def _iter_jsonl(stdout: str):
    """逐行产出 stdout 里的 JSON 对象；坏行/空行跳过（子代理输出不可信）。"""
    for ln in (stdout or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            yield obj


def _tool_result_text(content) -> str:
    """tool_result 内容块 → 纯文本（str / text 块列表两种形态都收）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text") or "")
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _num0(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _claude_usage(raw_usage: dict) -> dict:
    if not raw_usage:
        return {}
    cache_read = _num0(raw_usage.get("cache_read_input_tokens"))
    cache_creation = _num0(raw_usage.get("cache_creation_input_tokens"))
    # Ginno normalization (usage.py): input_tokens is the WHOLE prompt,
    # cache portions included.
    return {
        "input_tokens": _num0(raw_usage.get("input_tokens")) + cache_read + cache_creation,
        "output_tokens": _num0(raw_usage.get("output_tokens")),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }


def _pi_usage(raw: dict) -> dict:
    # pi 字段：input / output / cacheRead / cacheWrite（json.md v3）。
    return {
        "input_tokens": _num0(raw.get("input")) + _num0(raw.get("cacheRead")),
        "output_tokens": _num0(raw.get("output")),
        "cache_read_tokens": _num0(raw.get("cacheRead")),
        "cache_creation_tokens": _num0(raw.get("cacheWrite")),
    }


def _dsh_usage(raw: dict) -> dict:
    """dsh ``step_end`` 的 usage → ginno 四元组（一次调用可能多段，调用方累加）。

    dsh 字段：inputTokens / outputTokens / cacheReadTokens / cacheWriteTokens
    / totalTokens（totalTokens ≈ input+cache+output，不是独立账目，忽略）。
    inputTokens 与 Anthropic 口径一致，不含缓存段——ginno 归一化后
    input_tokens 是整段提示词（缓存段并入）。
    """
    cache_read = _num0(raw.get("cacheReadTokens"))
    cache_write = _num0(raw.get("cacheWriteTokens"))
    return {
        "input_tokens": _num0(raw.get("inputTokens")) + cache_read + cache_write,
        "output_tokens": _num0(raw.get("outputTokens")),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
    }
