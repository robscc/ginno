"""Model invocation helpers for the main agent graph.

Split out of ``graph.py`` (structural refactor, no behavior change). Every
name here is re-exported from ``ginno_runtime.graph`` so existing callers and
tests keep importing it from there.

``_ainvoke_rate_limited`` deliberately stays in ``graph.py``: its module
global ``_RATE_LIMIT_WAIT_S`` is monkeypatched through
``ginno_runtime.graph`` by tests, so the reader and the constant must live in
the same module.
"""

from __future__ import annotations

from langchain_core.messages import SystemMessage

from .world_state import context_settings


def is_retryable_error(exc: Exception) -> bool:
    """P5-3 retry policy: only transient failures are worth another attempt.

    Status code wins over message text. A deterministic 4xx (400/401/403/404/
    422) is NEVER retried — retrying it burns latency and quota without ever
    succeeding (the 2026-10-23 400 storms). 408/409/429 and 5xx are transient.
    Text matching is the fallback for wrapped errors without ``status_code``.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (408, 409, 429) or status >= 500:
            return True
        if 400 <= status < 500:
            return False
    text = f"{type(exc).__name__} {exc}".lower()
    # Non-retryable shapes first: an error that says "invalid request" is
    # deterministic even if its prose happens to mention a rate limit.
    if any(
        m in text
        for m in ("invalid_request", "badrequest", "authentication", "permission", "not found")
    ):
        return False
    return any(
        m in text
        for m in ("429", "rate limit", "too many requests", "timeout", "timed out",
                  "connection", "connecterror", "service unavailable", "overloaded")
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    """Backoff trigger for the in-turn retry: 429 only (see is_retryable_error).

    2026-10-27 起 backoff 不再静默：每轮等待都经 get_stream_writer 把
    provider 的**原始错误全文**透传到前端（e48be8a3 实录——429/1311
    「套餐无模型权限」在后台白等 30/60/120s，用户只见「卡死」）。是否
    继续等由用户看着原始错误决定（⏹ 即停）。"""
    if not is_retryable_error(exc):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429
    text = f"{type(exc).__name__} {exc}".lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _is_anthropic_model(model) -> bool:
    """Detect ChatAnthropic without a hard import (works on bound models too)."""
    target = getattr(model, "bound", model)  # RunnableBinding from bind_tools
    cls = type(target)
    return cls.__module__.startswith("langchain_anthropic")


def _system_message(sys_text: str, model) -> SystemMessage:
    """Wrap the stable system layer; on Anthropic attach a cache_control
    breakpoint (plan B3) so system + cached history prefix bill at cache
    rates. Skipped when disabled via settings.context.cache_control."""
    if _is_anthropic_model(model) and context_settings().get("cache_control", True):
        return SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": sys_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
    return SystemMessage(content=sys_text)


# B3-tail — rolling history breakpoints. With only the system-layer
# breakpoint the gateway cached just tools+system (~30k) while the growing
# history (up to 200k+) re-billed in full every call — the 2026-08 diagnosis
# showed cache_read frozen at the system+tools size for every session. Two
# breakpoints on the last content-bearing messages extend the cached prefix to
# the tail each call (total budget: system 1 + tail 2 = 3 of Anthropic's 4).
CACHE_TAIL_MARKS = 2
_CACHE_MARK = {"cache_control": {"type": "ephemeral"}}


def _with_cache_mark(content):
    """Copy of ``content`` with cache_control on its last block, or None when
    it cannot carry a mark (empty / unknown block types)."""
    if isinstance(content, str):
        if not content.strip():
            return None
        return [{"type": "text", "text": content, **_CACHE_MARK}]
    if isinstance(content, list) and content:
        blocks: list = []
        for b in content:
            if isinstance(b, dict):
                blocks.append(dict(b))
            elif isinstance(b, str):
                if b:
                    blocks.append({"type": "text", "text": b})
            else:
                return None  # non-dict block object — leave this message alone
        if not blocks:
            return None
        blocks[-1] = {**blocks[-1], **_CACHE_MARK}
        return blocks
    return None


def _mark_cache_tail(history: list, marks: int = CACHE_TAIL_MARKS) -> list:
    """Attach ephemeral cache_control breakpoints to the last content block of
    up to ``marks`` of the most recent content-bearing messages (rolling tail,
    Claude-Code style).

    Every model call appends to the history, so request N's tail breakpoint
    writes a prefix covering everything up to message M; request N+1 reads
    that prefix (its new breakpoints sit further along) — steady state caches
    the whole conversation except the newest append. Messages are COPIED
    (never mutated) so the persisted state keeps no cache marks.

    Marks must stay inside the region microcompact leaves alone (the most
    recent ``compact_keep_turns`` turns): the tail walker only ever touches
    the last few messages, so a turn-entry microcompact rewrite of OLDER
    messages never collides with a mark position.
    """
    out = list(history)
    remaining = marks
    for i in range(len(out) - 1, -1, -1):
        if remaining <= 0:
            break
        m = out[i]
        new_content = _with_cache_mark(getattr(m, "content", None))
        if new_content is None:
            continue  # empty/unmarkable content (e.g. AI tool_call stubs)
        out[i] = m.model_copy(update={"content": new_content})
        remaining -= 1
    return out
