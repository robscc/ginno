"""Build fingerprint — 让 /api/health 能回答「跑的是哪个 bundle」。

打包时 Makefile 把 build-info.json（git sha + dirty 标记 + channel）打进
bundle 根（``--add-data``，落在 ``sys._MEIPASS``）；dev 源码跑时回退到
git 探测，再回退包版本。runtime 热切换（docs/runtime-hot-swap-design.md）
用这个指纹验证「新 bundle 真的起来了」。
"""

from __future__ import annotations

import json
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
        return {
            "fingerprint": str(info["fingerprint"]),
            "channel": str(info.get("channel", "app")),
            "built_at": info.get("built_at"),
        }
    git = _git_describe()
    return {
        "fingerprint": git or _FALLBACK_VERSION,
        "channel": "dev" if git else "unknown",
        "built_at": None,
    }


def fingerprint() -> str:
    return build_info()["fingerprint"]
