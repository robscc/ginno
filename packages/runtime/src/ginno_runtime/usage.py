"""Model usage telemetry (plan D1/D4 + usage-stats-design.md §3.5).

Extracts token usage from LangChain ``AIMessage.usage_metadata`` — populated by
ChatOpenAI / ChatAnthropic from the provider response — and maintains a small
per-session accumulator so the WS layer can report both per-call and cumulative
numbers, including prompt-cache hit rates (D4).

Canonical (normalized) shape — usage-stats-design.md §3.5. Providers report
cache fields differently, so extraction normalizes at the boundary and every
downstream consumer (WS events, TopBar, usage logs, aggregates) sees the SAME
semantics:

* ``input_tokens``          WHOLE prompt = non-cached input + cache read +
                            cache creation. Anthropic's raw ``input_tokens``
                            excludes the cached portions, so they are added
                            back; OpenAI's ``prompt_tokens`` already includes
                            cached tokens and passes through unchanged.
* ``output_tokens``         as reported.
* ``cache_read_tokens``     prompt-cache hits (Anthropic ``cache_read`` or
                            OpenAI ``cached_tokens``).
* ``cache_creation_tokens`` cache writes (Anthropic only; 0 elsewhere).

With this shape the hit ratio ``cache_read / input`` is always in [0, 1] and
comparable across providers (the pre-normalization Anthropic denominator
excluded cache tokens, which could push the ratio past 100%).

``usage_metadata`` shape (langchain-core)::

    {"input_tokens": int, "output_tokens": int, "total_tokens": int,
     "input_token_details": {"cache_read": int|None, "cache_creation": int|None,
                             "cached_tokens": int|None}}
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
    """Return normalized usage for one AI message, or None when absent.

    See the module docstring for the canonical field semantics: the returned
    ``input_tokens`` is the WHOLE prompt (cache portions included), so the
    cache hit ratio can never exceed 100%.
    """
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

    raw_input = _num(_field(um, "input_tokens"))
    # Anthropic-style details carry cache_read / cache_creation; OpenAI-style
    # carry cached_tokens (prompt_tokens already INCLUDES the cached part).
    has_anthropic_details = (
        _field(details, "cache_read") is not None
        or _field(details, "cache_creation") is not None
    )
    cache_read = _num(_field(details, "cache_read")) or _num(_field(details, "cached_tokens"))
    cache_creation = _num(_field(details, "cache_creation"))
    if has_anthropic_details:
        # raw input excludes cached portions → rebuild the whole-prompt count
        input_tokens = raw_input + cache_read + cache_creation
    else:
        input_tokens = raw_input
    return {
        "input_tokens": input_tokens,
        "output_tokens": _num(_field(um, "output_tokens")),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
    }


def add_usage(acc: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    """Accumulate one call's usage into a session accumulator (in place)."""
    for k in USAGE_FIELDS:
        acc[k] = acc.get(k, 0) + int(usage.get(k, 0))
    acc["calls"] = acc.get("calls", 0) + 1
    return acc


def cache_hit_ratio(acc: dict[str, int]) -> float:
    """cache_read / whole-prompt input (usage-stats-design.md §3.5).

    ``input_tokens`` is normalized to include the cached portions, so the
    ratio is a true share of the prompt and is always in [0, 1]. 0.0 when
    there was no input yet.
    """
    inp = acc.get("input_tokens", 0)
    if inp <= 0:
        return 0.0
    return round(acc.get("cache_read_tokens", 0) / inp, 4)
