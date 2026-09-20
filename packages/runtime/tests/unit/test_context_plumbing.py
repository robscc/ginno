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


def test_extract_usage_cache_details_anthropic_normalizes_input():
    # langchain 1.x ChatAnthropic ALREADY rebuilds the whole-prompt count into
    # input_tokens (raw 100 + 60 read + 10 creation = 170 before it reaches
    # us), so extraction passes it through unchanged — adding the cache fields
    # again double-counted every cached token (2026-08 cache-rate diagnosis).
    meta = {"input_tokens": 170, "output_tokens": 20, "total_tokens": 190,
            "input_token_details": {"cache_read": 60, "cache_creation": 10}}
    u = extract_usage(AIMessage(content="x", usage_metadata=meta))
    assert u["input_tokens"] == 170
    assert u["cache_read_tokens"] == 60
    assert u["cache_creation_tokens"] == 10


def test_extract_usage_ephemeral_creation_buckets():
    # When the provider returns a cache_creation TTL breakdown, langchain
    # zeroes the generic cache_creation field and moves the amounts into
    # ephemeral_5m/1h keys — extraction must recover the write count from
    # them (observed live against the local Qwen gateway: every write was
    # logged as 0 before this was handled).
    meta = {"input_tokens": 1897, "output_tokens": 12, "total_tokens": 1909,
            "input_token_details": {"cache_creation": 0, "cache_read": 0,
                                    "ephemeral_5m_input_tokens": 1891}}
    u = extract_usage(AIMessage(content="x", usage_metadata=meta))
    assert u["input_tokens"] == 1897
    assert u["cache_read_tokens"] == 0
    assert u["cache_creation_tokens"] == 1891

    meta_1h = {"input_tokens": 500, "output_tokens": 5, "total_tokens": 505,
               "input_token_details": {"cache_creation": 0, "cache_read": 100,
                                       "ephemeral_5m_input_tokens": 200,
                                       "ephemeral_1h_input_tokens": 50}}
    u = extract_usage(AIMessage(content="x", usage_metadata=meta_1h))
    assert u["cache_creation_tokens"] == 250
    assert u["cache_read_tokens"] == 100


def test_extract_usage_openai_cached_tokens():
    # OpenAI's prompt_tokens ALREADY include the cached part; cached_tokens
    # maps onto cache_read and input passes through unchanged.
    meta = {"input_tokens": 200, "output_tokens": 30, "total_tokens": 230,
            "input_token_details": {"cached_tokens": 80}}
    u = extract_usage(AIMessage(content="x", usage_metadata=meta))
    assert u["input_tokens"] == 200
    assert u["cache_read_tokens"] == 80
    assert u["cache_creation_tokens"] == 0


def test_extract_usage_none_without_metadata():
    assert extract_usage(AIMessage(content="x")) is None


def test_hit_ratio_never_exceeds_one():
    # Whole-prompt denominator keeps the ratio in [0, 1] even for an
    # almost-fully-cached call (the pre-normalization >100% bug).
    acc = empty_usage()
    add_usage(acc, {"input_tokens": 1000, "output_tokens": 10,
                    "cache_read_tokens": 990, "cache_creation_tokens": 0})
    assert cache_hit_ratio(acc) == 0.99


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


def test_turn_context_carries_bound_workflow(isolated_home):
    from ginno_runtime.graph import build_turn_context

    out = build_turn_context(
        query="",
        bound_workflow={
            "id": "790382cc6f",
            "name": "cluster_checklist_review",
            "version": 1,
            "description": "review a cluster checklist",
            "dsl": {
                "entry": "parse",
                "nodes": [
                    {"id": "parse", "type": "step", "goal": "read"},
                    {"id": "gate", "type": "human", "question": "ok?"},
                ],
                "edges": [{"from": "parse", "to": "gate"}],
            },
        },
    )
    assert "<bound_workflow>" in out
    assert "790382cc6f" in out
    assert "gate=human" in out
    assert '"type": "human"' in out


def test_turn_context_carries_discovered_projects(isolated_home):
    """A repo the agent only reached by absolute path must be visible, and the
    ambiguity must be flagged before the model picks a target."""
    from ginno_runtime.graph import build_turn_context

    out = build_turn_context(
        query="",
        projects=[
            {
                "path": "/Users/me/work/claude-agent-team",
                "name": "claude-agent-team",
                "markers": [".git", ".claude", "CLAUDE.md"],
                "has_claude": True,
                "claude_skills": 14,
            }
        ],
    )
    assert "<projects>" in out
    assert "/Users/me/work/claude-agent-team" in out
    assert "14 个 skill" in out
    assert "/Users/me/work/claude-agent-team/.claude/skills" in out
    assert "ask_user" in out  # the ask-first rule


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


# --------------------------------------------------------------------------- #
# B3-tail — rolling history cache breakpoints
# --------------------------------------------------------------------------- #
def test_cache_tail_marks_last_two_content_messages():
    from langchain_core.messages import HumanMessage, ToolMessage

    from ginno_runtime.graph import _mark_cache_tail

    h = [
        HumanMessage(content="first"),
        ToolMessage(content="tool out", tool_call_id="c1", id="t1"),
        HumanMessage(content="second", id="h2"),
    ]
    out = _mark_cache_tail(h)
    # marks on the last two content-bearing messages, none on the first
    assert out[0].content == "first"
    assert out[1].content == [
        {"type": "text", "text": "tool out", "cache_control": {"type": "ephemeral"}}
    ]
    assert out[2].content == [
        {"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}
    ]
    # originals untouched (send-only copies, like strip_old_images)
    assert h[1].content == "tool out" and h[2].content == "second"


def test_cache_tail_skips_empty_content_and_list_blocks():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from ginno_runtime.graph import _mark_cache_tail

    h = [
        HumanMessage(content="early"),
        AIMessage(content="", tool_calls=[
            {"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content=[{"type": "text", "text": "result"}], tool_call_id="c1"),
    ]
    out = _mark_cache_tail(h)
    # empty-content AI stub skipped; list content marked on its LAST block
    assert out[1].content == ""
    assert out[2].content == [
        {"type": "text", "text": "result", "cache_control": {"type": "ephemeral"}}
    ]
    assert out[0].content == [
        {"type": "text", "text": "early", "cache_control": {"type": "ephemeral"}}
    ]


def test_cache_tail_mark_copies_blocks():
    from langchain_core.messages import HumanMessage

    from ginno_runtime.graph import _mark_cache_tail

    img = {"type": "image", "source": {"data": "abc"}}
    orig = HumanMessage(content=[{"type": "text", "text": "look"}, img])
    out = _mark_cache_tail([orig])
    marked = out[0].content
    assert marked[-1] == {"type": "image", "source": {"data": "abc"},
                          "cache_control": {"type": "ephemeral"}}
    # the original block dicts are not mutated in place
    assert "cache_control" not in orig.content[0]
    assert "cache_control" not in orig.content[1]
    assert marked[0] is not orig.content[0]


def test_cache_tail_empty_history():
    from ginno_runtime.graph import _mark_cache_tail

    assert _mark_cache_tail([]) == []
