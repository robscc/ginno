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
from functools import lru_cache
from pathlib import Path

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


@lru_cache(maxsize=1)
def _encoder():
    try:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(_cache_dir()))
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 — counting is advisory, never fatal
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