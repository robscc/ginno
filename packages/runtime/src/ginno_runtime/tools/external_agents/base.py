"""External-agent delegation base: interface + IR + shared helpers.

一个 CLI 一个 adapter（claude_code.py / codex.py / pi.py，connector-module
同款模式），全部实现这里的 ExternalBackend 接口。本文件只放接口与共用
设施：结果类型（DelegationResult，含转录 IR）、JSONL 迭代与 usage 映射、
CLI 路径解析（GUI PATH 兜底）、宽松 JSON 解析。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Protocol


DELEGATE_TOOL_NAME = "delegate_agent"

# Delegation runs an entire agent loop — give it real headroom, but never
# let a stalled child sit for half an hour.
_TIMEOUT_MIN_S = 10
_TIMEOUT_MAX_S = 1800
_TIMEOUT_DEFAULT_S = 1800  # 2026-10-05：600→1800（用户要求）；0=永不超时

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

    def available(self) -> str | None:
        """Absolute path of the CLI, or None when not installed."""
        ...

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        ...

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
