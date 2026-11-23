"""Local token counting for the context panel (2026-11-13 P0).

The panel needs to answer "where did my context go?" without calling a
provider. ``tiktoken`` with ``o200k_base`` is the best local approximation we
can ship across Claude / Qwen / DeepSeek / OpenAI; the last real
``prompt_tokens`` recorded for the session is used to calibrate the fixed
parts (see ``context_breakdown``). All numbers exposed to the UI are ``≈``.

Offline / frozen builds: ``tiktoken`` normally downloads the BPE vocab from
openaipublic on first use. The app build bundles it under
``tiktoken_cache/<sha1-of-url>`` (see ``scripts/fetch_tiktoken_vocab.py`` and
the Makefile); when the vocab is missing we fall back to a CJK-aware char
estimate so the panel never breaks.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

# 加载 tiktoken 词表的硬超时（秒）：冷缓存时 tiktoken.get_encoding 会从
# openaipublic.blob 下载，无超时控制；TUN/断网环境下 socket 永久阻塞会
# 拖死调用方（曾把 pytest worker 卡到 120s 被 pytest-timeout 杀掉）。
_LOAD_TIMEOUT_S = 10.0
# 失败/超时后的冷却窗口（秒）：避免每个请求都重新发起注定失败的加载。
_OFFLINE_COOLDOWN_S = 600.0
_offline_until = 0.0

# CJK + kana + hangul: ~1 token per character. Latin/digits/code: ~3.6 chars
# per token (calibrated against mixed Chinese/code prompts; only used when
# tiktoken is unavailable).
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _bundle_cache_dir() -> Path | None:
    """tiktoken cache shipped inside the PyInstaller onedir bundle."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = Path(base) / "tiktoken_cache"
        if cand.is_dir():
            return cand
    return None


def _cache_dir() -> Path:
    env = os.environ.get("TIKTOKEN_CACHE_DIR")
    if env:
        return Path(env)
    bundled = _bundle_cache_dir()
    if bundled is not None:
        return bundled
    from . import paths

    p = paths.home() / "cache" / "tiktoken"
    p.mkdir(parents=True, exist_ok=True)
    return p


_cached_encoder: object | None = None
_encoder_loaded = False


def _encoder():
    global _cached_encoder, _encoder_loaded
    if _encoder_loaded:
        return _cached_encoder
    if time.monotonic() < _offline_until:
        return None
    try:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_cache_dir()))
        import tiktoken

        result: list = []

        def _load() -> None:
            result.append(tiktoken.get_encoding("o200k_base"))

        worker = threading.Thread(target=_load, daemon=True)
        worker.start()
        worker.join(_LOAD_TIMEOUT_S)
        if worker.is_alive() or not result:
            # 超时（下载卡死）或加载失败：进入冷却窗口，期间直接回退估算。
            globals()["_offline_until"] = time.monotonic() + _OFFLINE_COOLDOWN_S
            return None
        _cached_encoder = result[0]
        _encoder_loaded = True
        return _cached_encoder
    except Exception:  # noqa: BLE001 — counting is advisory, never fatal
        globals()["_offline_until"] = time.monotonic() + _OFFLINE_COOLDOWN_S
        return None


def count_tokens(text: str) -> int:
    """Best-effort token count; never raises."""
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            # disallowed_special=() so user/tool text containing the special
            # token literal does not raise.
            return len(enc.encode(text, disallowed_special=()))
        except Exception:  # noqa: BLE001
            pass
    cjk = len(_CJK.findall(text))
    return int(cjk + (len(text) - cjk) / 3.6)


def counter_name() -> str:
    """``tiktoken`` when the real encoder loaded, else ``estimate``."""
    return "tiktoken" if _encoder() is not None else "estimate"