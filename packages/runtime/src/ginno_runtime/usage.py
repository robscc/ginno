"""Model usage telemetry (plan D1/D4).

Extracts token usage from LangChain ``AIMessage.usage_metadata`` — populated by
ChatOpenAI / ChatAnthropic from the provider response — and maintains a small
per-session accumulator so the WS layer can report both per-call and cumulative
numbers, including prompt-cache hit rates (D4).

``usage_metadata`` shape (langchain-core)::

    {"input_tokens": int, "output_tokens": int, "total_tokens": int,
     "input_token_details": {"cache_read": int|None, "cache_creation": int|None}}
"""

from __future__ import annotations

from typing import Any

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def empty_usage() -> dict[str, int]:
    return {k: 0 for k in USAGE_FIELDS} | {"calls": 0}


def extract_usage(message: Any) -> dict[str, int] | None:
    """Return normalized usage for one AI message, or None when absent."""
    um = getattr(message, "usage_metadata", None)
    if not um:
        return None
    try:
        details = um.get("input_token_details") or {}
    except AttributeError:
        details = getattr(um, "input_token_details", None) or {}

    def _num(v: Any) -> int:
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _field(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    return {
        "input_tokens": _num(_field(um, "input_tokens")),
        "output_tokens": _num(_field(um, "output_tokens")),
        "cache_read_tokens": _num(_field(details, "cache_read")),
        "cache_creation_tokens": _num(_field(details, "cache_creation")),
    }


def add_usage(acc: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    """Accumulate one call's usage into a session accumulator (in place)."""
    for k in USAGE_FIELDS:
        acc[k] = acc.get(k, 0) + int(usage.get(k, 0))
    acc["calls"] = acc.get("calls", 0) + 1
    return acc


def cache_hit_ratio(acc: dict[str, int]) -> float:
    """cache_read / input (D4). 0.0 when there was no input yet."""
    inp = acc.get("input_tokens", 0)
    if inp <= 0:
        return 0.0
    return round(acc.get("cache_read_tokens", 0) / inp, 4)
