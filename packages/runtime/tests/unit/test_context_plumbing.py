"""Unit tests for the context-engineering plumbing: token estimation (E1),
usage extraction (D1/D4), middle truncation (E2), and the stable system /
turn-context split (B1/B2/B3)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from ginno_runtime.tokens import (
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from ginno_runtime.truncation import TRUNCATION_MARKER, truncate_middle, truncate_tool_content
from ginno_runtime.usage import add_usage, cache_hit_ratio, empty_usage, extract_usage

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# E1 — token estimation
# --------------------------------------------------------------------------- #
def test_estimate_empty():
    assert estimate_text_tokens("") == 0


def test_estimate_monotonic():
    short = "hello world"
    long = short * 100
    assert estimate_text_tokens(long) > estimate_text_tokens(short)


def test_estimate_cjk_denser_per_char_than_ascii():
    cjk = estimate_text_tokens("中" * 100)
    ascii_ = estimate_text_tokens("a" * 100)
    assert cjk > ascii_  # CJK chars count ~1.5 tokens each


def test_estimate_messages_includes_tool_calls():
    plain = AIMessage(content="hi")
    with_calls = AIMessage(
        content="hi",
        tool_calls=[{"name": "bash", "args": {"cmd": "x" * 500}, "id": "c1", "type": "tool_call"}],
    )
    assert estimate_message_tokens(with_calls) > estimate_message_tokens(plain)
    assert estimate_messages_tokens([plain, with_calls]) == (
        estimate_message_tokens(plain) + estimate_message_tokens(with_calls)
    )


# --------------------------------------------------------------------------- #
# D1/D4 — usage extraction + accumulation
# --------------------------------------------------------------------------- #
def _ai_with_usage(**details) -> AIMessage:
    meta = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    if details:
        meta["input_token_details"] = details
    return AIMessage(content="x", usage_metadata=meta)


def test_extract_usage_basic():
    u = extract_usage(_ai_with_usage())
    assert u == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_extract_usage_cache_details():
    u = extract_usage(_ai_with_usage(cache_read=60, cache_creation=10))
    assert u["cache_read_tokens"] == 60
    assert u["cache_creation_tokens"] == 10


def test_extract_usage_none_without_metadata():
    assert extract_usage(AIMessage(content="x")) is None


def test_accumulator_and_hit_ratio():
    acc = empty_usage()
    add_usage(acc, {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 80, "cache_creation_tokens": 0})
    add_usage(acc, {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 90, "cache_creation_tokens": 5})
    assert acc["input_tokens"] == 200
    assert acc["cache_read_tokens"] == 170
    assert acc["calls"] == 2
    assert cache_hit_ratio(acc) == 0.85
    assert cache_hit_ratio(empty_usage()) == 0.0


# --------------------------------------------------------------------------- #
# E2 — middle truncation
# --------------------------------------------------------------------------- #
def test_truncate_short_unchanged():
    assert truncate_middle("abc", 100) == "abc"


def test_truncate_middle_keeps_head_tail_and_marks():
    text = "H" * 1000 + "M" * 5000 + "T" * 1000
    out = truncate_middle(text, max_chars=1000)
    assert TRUNCATION_MARKER in out
    assert out.startswith("H" * 600)  # head kept (default 0.6 ratio of 1000)
    assert out.rstrip().endswith("T" * 400)  # tail kept (remaining budget)
    assert "M" * 100 not in out  # middle dropped
    assert "原文 7000" in out  # original size recorded


def test_truncate_tool_content_non_str_passthrough():
    payload = [{"type": "text", "text": "x" * 99999}]
    assert truncate_tool_content(payload, 10) is payload


# --------------------------------------------------------------------------- #
# B1/B2/B3 — stable system vs turn context, cache_control
# --------------------------------------------------------------------------- #
def _agent_stub():
    from ginno_runtime.agents.registry import AgentConfig

    return AgentConfig(id="t", name="T", system_prompt="You are T.", tools_allow=["*"])


def test_stable_system_has_sections_no_volatile(isolated_home):
    from ginno_runtime.graph import build_stable_system

    text = build_stable_system(_agent_stub(), "default", [], agent_id="t")
    assert "You are T." in text
    assert "operating in this turn as **T**" in text
    assert "<environment>" in text
    assert "<permissions>" in text
    # nothing query-dependent leaks into the stable layer
    assert "<injected_wiki>" not in text
    assert "<attached_files>" not in text


def test_stable_system_byte_identical_across_calls(isolated_home):
    from ginno_runtime.graph import build_stable_system

    a = build_stable_system(_agent_stub(), "default", [], agent_id="t")
    b = build_stable_system(_agent_stub(), "default", [], agent_id="t")
    assert a == b  # B2: deterministic → prefix-cache friendly


def test_turn_context_carries_volatile(isolated_home):
    from ginno_runtime.graph import build_turn_context

    out = build_turn_context(
        query="",
        attached_files=[{"name": "a.csv", "path": "/tmp/a.csv", "kind": "table"}],
        mention_context=[{"kind": "workflow", "id": "w1", "name": "wf", "summary": "s"}],
    )
    assert "<attached_files>" in out and "a.csv" in out
    assert "<mentioned_workflow>" in out


def test_turn_context_empty_when_nothing(isolated_home):
    from ginno_runtime.graph import build_turn_context

    assert build_turn_context(query="", attached_files=None, mention_context=None) == ""


def test_cache_control_only_for_anthropic():
    from ginno_runtime.graph import _is_anthropic_model, _system_message

    class FakeAnthropic:
        pass

    FakeAnthropic.__module__ = "langchain_anthropic.chat_models"

    class FakeOpenAI:
        pass

    FakeOpenAI.__module__ = "langchain_openai.chat_models"

    assert _is_anthropic_model(FakeAnthropic())
    assert not _is_anthropic_model(FakeOpenAI())

    msg = _system_message("SYS", FakeAnthropic())
    assert isinstance(msg.content, list)
    assert msg.content[0]["cache_control"] == {"type": "ephemeral"}

    msg2 = _system_message("SYS", FakeOpenAI())
    assert msg2.content == "SYS"
