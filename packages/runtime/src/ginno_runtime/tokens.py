"""Token estimation (plan E1).

Local heuristic used for compaction thresholds and budget decisions between
turns. The ground truth is the provider's usage counters (see ``usage.py``);
this module only needs to be *monotonic and cheap*, not exact — Codex uses the
same bytes/4 rule of thumb for its local estimates.

CJK text is denser than the latin 4-bytes-per-token average, so CJK characters
count ~1.5 tokens each (conservative side: over-estimating triggers compaction
a little earlier, which is the safe direction).
"""

from __future__ import annotations

import json
from typing import Any

_BYTES_PER_TOKEN = 4
_CJK_TOKENS_PER_CHAR = 1.5


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF  # CJK unified ideographs
        or 0x3400 <= o <= 0x4DBF  # extension A
        or 0x3040 <= o <= 0x30FF  # kana
        or 0xAC00 <= o <= 0xD7AF  # hangul
    )


def estimate_text_tokens(text: str) -> int:
    """Cheap token estimate for a text blob. Empty → 0."""
    if not text:
        return 0
    cjk = 0
    latin_chars = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            latin_chars += 1
    # latin/other: approximate by char count (close to bytes for ASCII; mild
    # under-count for multi-byte accents is acceptable at this precision).
    return int(cjk * _CJK_TOKENS_PER_CHAR + latin_chars / _BYTES_PER_TOKEN) + 1


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("thinking") or ""
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def estimate_message_tokens(message: Any) -> int:
    """Estimate one LangChain message's token cost (content + tool calls)."""
    total = 4  # per-message role/format overhead
    total += estimate_text_tokens(_content_text(getattr(message, "content", "")))
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        try:
            total += estimate_text_tokens(json.dumps(tool_calls, ensure_ascii=False, default=str))
        except Exception:
            total += 64
    return total


def estimate_messages_tokens(messages: list[Any]) -> int:
    return sum(estimate_message_tokens(m) for m in messages or [])
