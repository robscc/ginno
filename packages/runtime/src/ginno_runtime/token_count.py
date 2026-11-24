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

# 调用者最多等待这么久。冷缓存时 tiktoken 的下载可能一直阻塞，
# 只要原加载仍在途，就复用它，即使冷却期已经结束。
_LOAD_TIMEOUT_S = 10.0
_OFFLINE_COOLDOWN_S = 600.0
_offline_until = 0.0
_load_lock = threading.Lock()
_load_done: threading.Event | None = None

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


def _load_encoder(done: threading.Event) -> None:
    global _cached_encoder, _encoder_loaded, _load_done, _offline_until
    encoder = None
    try:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_cache_dir()))
        import tiktoken

        encoder = tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 — counting is advisory, never fatal
        pass
    finally:
        with _load_lock:
            if encoder is None:
                _offline_until = time.monotonic() + _OFFLINE_COOLDOWN_S
            else:
                _cached_encoder = encoder
                _encoder_loaded = True
                _offline_until = 0.0
            _load_done = None
            done.set()


def _encoder():
    global _load_done, _offline_until
    with _load_lock:
        if _encoder_loaded:
            return _cached_encoder
        if time.monotonic() < _offline_until:
            return None
        if _load_done is None:
            _load_done = threading.Event()
            try:
                threading.Thread(target=_load_encoder, args=(_load_done,), daemon=True).start()
            except Exception:  # noqa: BLE001 — thread startup may fail
                _load_done = None
                _offline_until = time.monotonic() + _OFFLINE_COOLDOWN_S
                return None
        done = _load_done

    done.wait(_LOAD_TIMEOUT_S)
    with _load_lock:
        if _encoder_loaded:
            return _cached_encoder
        if _load_done is done:
            # Only an unfinished load can enter cooldown here. Keep its event
            # registered so a timeout cannot launch a second worker.
            now = time.monotonic()
            if now >= _offline_until:
                _offline_until = now + _OFFLINE_COOLDOWN_S
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