"""Build fingerprint — 让 /api/health 能回答「跑的是哪个 bundle」。

打包时 Makefile 把 build-info.json（git sha + dirty 标记 + channel）打进
bundle 根（``--add-data``，落在 ``sys._MEIPASS``）；dev 源码跑时回退到
git 探测，再回退包版本。runtime 热切换（docs/runtime-hot-swap-design.md）
用这个指纹验证「新 bundle 真的起来了」。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

_FALLBACK_VERSION = "0.0.0+unknown"


def _read_bundled_info() -> dict | None:
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    p = Path(base) / "build-info.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _git_describe() -> str | None:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[4],
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[4],
        ).stdout.strip()
        if sha:
            return f"{sha}{'-dirty' if dirty else ''}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@lru_cache(maxsize=1)
def build_info() -> dict:
    """{"fingerprint": str, "channel": str, "built_at": str|None}。"""
    info = _read_bundled_info()
    if info and info.get("fingerprint"):
        out = {
            "fingerprint": str(info["fingerprint"]),
            "channel": str(info.get("channel", "app")),
            "built_at": info.get("built_at"),
        }
    else:
        git = _git_describe()
        out = {
            "fingerprint": git or _FALLBACK_VERSION,
            "channel": "dev" if git else "unknown",
            "built_at": None,
        }
    # 壳的 spawn ladder 从 dev-override 起 sidecar 时注入 GINNO_RUNTIME_CHANNEL
    # =dev：channel 是「本进程从哪被拉起」的运行时事实——build-info.json 写死
    # 的是构建目标（make runtime/app 同一产物，永远是 app），无法区分热切换
    # bundle 与 factory（2026-11-04：前端 dev·徽标因此从未可靠亮过）。
    env_channel = os.environ.get("GINNO_RUNTIME_CHANNEL")
    if env_channel:
        out["channel"] = env_channel
    return out


def fingerprint() -> str:
    return build_info()["fingerprint"]
