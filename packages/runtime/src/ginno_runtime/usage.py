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


def context_state(last_input_tokens: int, provider: str = "", model: str = "") -> dict:
    """Context-window occupancy for the UI ring (TopBar 圆环).

    ``last_input_tokens`` = the WHOLE prompt of the most recent LLM call
    (already includes cache read/creation per the normalized shape) — the
    best proxy for "current context size". Window resolution mirrors
    mods/ops._op_usage: the config's explicit context_window → model-name
    lookup → 200k fallback (token-weather's contract). Never raises.
    """
    try:
        from .providers import context_window_for, get_config

        window = context_window_for(
            get_config(provider) if provider else None, model
        ) or 200_000
    except Exception:  # noqa: BLE001 — 显示辅助，解析失败走兜底窗口
        window = 200_000
    tokens = max(0, int(last_input_tokens or 0))
    return {
        "tokens": tokens,
        "window": window,
        "percent": round(tokens * 100 / window, 1) if window > 0 else 0.0,
    }


# 「当前上下文」只统计跑过会话 graph 的真实对话调用。绕过 graph 的直连调用
# （压缩摘要/标题/探活/记忆/外部 agent/workflow 内部 agent/subagent 调度/分类
# 器/mods 统计）的 input 是被压全量、外部 prompt 或摘要 transcript——都不是
# 本会话上下文。ContextRing 锯齿根因（2026-11-07）：手动 /compact 的命令 turn
# 不跑 graph、没有对话 record，压缩 record（input=被压全量，如 360k/600k=60%）
# 成为最后一条 → 切会话时 GET /usage seed 出被压前的高占用，与 WS usage 帧的
# 压缩后真值（13%）交替闪烁。channels 投递（schedule/wecom/dingtalk/task/…）
# 走 run_user_turn → 真实上下文，不在名单内。
NON_CONTEXT_SOURCES = frozenset({
    "compaction", "external", "memory", "title", "probe", "classifier",
    "mods", "subagent_gate", "subagent_plan", "workflow",
})

# sideband 记录跨过的扫描窗口：足够跨过「手动 /compact 后 + 一串 sideband 调用」
# 的最后一段；连续 20+ 条全是 sideband 的会话视为无对话记录。
_CONTEXT_SCAN_ROWS = 20


def last_chat_input_tokens(session_id: str) -> int | None:
    """最近一次**对话调用**的全量 prompt tokens——TopBar 圆环同分子。

    compaction 的阈值判断以此为准（2026-11-07，会话 1a0c1d77：真实 491k
    显示 81% 却不触发——本地估算对工具输出/代码低估 ~20%，392k < 600k×
    0.75=450k 永远骑在线下）。返回 None = 扫描窗口内无对话记录（新会话
    /首次 turn），调用方回退本地估算。usage log 的 input_tokens 已是全量
    prompt（含缓存读/写，见模块 docstring 的规范化语义）。
    """
    try:
        from . import usage_store

        rows = (
            usage_store.query_requests(
                session_id=session_id, page=1, page_size=_CONTEXT_SCAN_ROWS
            ).get("rows")
            or []
        )
        row = next(
            (r for r in rows if r.get("source") not in NON_CONTEXT_SOURCES),
            None,
        )
        tokens = int((row or {}).get("input_tokens") or 0)
        return tokens if tokens > 0 else None
    except Exception:  # noqa: BLE001 — 阈值判断的辅助，失败回退估算
        return None


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
        # 思考 token 拆分(thinking-effort-design §8,best-effort):langchain
        # 1.x 规范键是 output_token_details.reasoning(OutputTokenDetails
        # TypedDict,OpenAI 系细项;anthropic thinking 同样归入);个别网关
        # 塞 reasoning_tokens——两种都认,拿不到就是 0。
        "reasoning_tokens": _num(_field(_output_details(um), "reasoning"))
        or _num(_field(_output_details(um), "reasoning_tokens")),
    }


def _output_details(um: Any) -> Any:
    """usage_metadata.output_token_details,dict/attr 两种形态通吃。"""
    if isinstance(um, dict):
        return um.get("output_token_details") or {}
    return getattr(um, "output_token_details", None) or {}


def add_usage(acc: dict[str, int], usage: dict[str, int]) -> dict[str, int]:
    """Accumulate one call's usage into a session accumulator (in place)."""
    for k in USAGE_FIELDS:
        acc[k] = acc.get(k, 0) + int(usage.get(k, 0))
    acc["calls"] = acc.get("calls", 0) + 1
    # reasoning_tokens 是 output 的子集,单独累加供 Stats 拆分显示。
    acc["reasoning_tokens"] = acc.get("reasoning_tokens", 0) + int(usage.get("reasoning_tokens", 0))
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


def record_message(
    message: Any,
    *,
    source: str,
    provider: str = "",
    model: str = "",
    session_id: str | None = None,
    agent_id: str | None = None,
    turn_id: str | None = None,
    latency_ms: int | None = None,
) -> dict[str, int] | None:
    """Extract + persist usage for one auxiliary LLM call (never raises).

    Every background call site (title generation, sub-agent planning/gating,
    team generation, …) funnels through here so a new source only needs the
    ``source`` tag — see docs/dream-mode-design.md §8. Returns the extracted
    usage dict, or None when the provider reported none.
    """
    try:
        from . import usage_store

        u = extract_usage(message)
        if not u:
            return None
        usage_store.record(
            input_tokens=u.get("input_tokens") or 0,
            output_tokens=u.get("output_tokens") or 0,
            cache_read_tokens=u.get("cache_read_tokens") or 0,
            cache_creation_tokens=u.get("cache_creation_tokens") or 0,
            reasoning_tokens=u.get("reasoning_tokens") or 0,
            provider=provider or "",
            model=model or "",
            source=source,
            session_id=session_id,
            agent_id=agent_id,
            turn_id=turn_id,
            latency_ms=latency_ms,
        )
        return u
    except Exception:  # noqa: BLE001 — telemetry never breaks the call path
        return None
