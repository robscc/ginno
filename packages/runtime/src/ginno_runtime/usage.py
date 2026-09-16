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
                            cache creation. The langchain 1.x chat models
                            already deliver this shape on BOTH providers:
                            ChatAnthropic adds the cached portions back into
                            ``input_tokens`` (see langchain_anthropic
                            ``_create_usage_metadata``), and ChatOpenAI passes
                            ``prompt_tokens`` through, which already includes
                            the cached tokens. So extraction passes the value
                            through unchanged. (Pre-1.0 langchain reported the
                            Anthropic raw count that excluded cached portions —
                            adding them back here would now double-count.)
* ``output_tokens``         as reported.
* ``cache_read_tokens``     prompt-cache hits (Anthropic ``cache_read`` or
                            OpenAI ``cached_tokens``).
* ``cache_creation_tokens`` cache writes. When the provider returns a TTL
                            breakdown (``cache_creation`` dict), langchain
                            zeroes the generic ``cache_creation`` field and
                            moves the amounts into ``ephemeral_5m_input_tokens``
                            / ``ephemeral_1h_input_tokens`` — so all three
                            fields are summed here (2026-08 gateway diagnosis:
                            reading only ``cache_creation`` logged every write
                            as 0).

With this shape the hit ratio ``cache_read / input`` is always in [0, 1] and
comparable across providers (the pre-normalization Anthropic denominator
excluded cache tokens, which could push the ratio past 100%).

``usage_metadata`` shape (langchain-core 1.x)::

    {"input_tokens": int, "output_tokens": int, "total_tokens": int,
     "input_token_details": {"cache_read": int|None, "cache_creation": int|None,
                             "cached_tokens": int|None,
                             "ephemeral_5m_input_tokens": int|None,
                             "ephemeral_1h_input_tokens": int|None}}
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

    # langchain 1.x already normalizes input_tokens to the WHOLE prompt for
    # both providers (ChatAnthropic adds the cached portions back in
    # _create_usage_metadata; ChatOpenAI passes prompt_tokens through, which
    # includes cached_tokens). Passing through here — adding the cache fields
    # again double-counted every cached token (2026-08 cache-rate diagnosis).
    input_tokens = _num(_field(um, "input_tokens"))
    cache_read = _num(_field(details, "cache_read")) or _num(_field(details, "cached_tokens"))
    # Cache writes: when the provider reports a TTL breakdown, langchain zeroes
    # the generic field and moves the amounts into the ephemeral_* keys — sum
    # all three so writes are never lost.
    cache_creation = (
        _num(_field(details, "cache_creation"))
        + _num(_field(details, "ephemeral_5m_input_tokens"))
        + _num(_field(details, "ephemeral_1h_input_tokens"))
    )
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
