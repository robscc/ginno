"""Thinking mode: ``enable_thinking`` → extra_body, ``reasoning_content`` restored.

Two layers, both no-network:

1. config passthrough — ``enable_thinking`` in the provider config lands in
   ``extra_body`` (model construction only, same approach as
   test_models_search.py).
2. langchain-openai 1.x drops non-standard delta fields (``reasoning_content``)
   from OpenAI-compatible gateways; our ChatOpenAI subclass must re-attach them
   to ``additional_kwargs`` so api/stream.py's ``thinking.delta`` hook and
   api/messages_ui.py's history replay fire again.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk

from ginno_runtime import providers as prov_mod
from ginno_runtime.models import build_model

pytestmark = pytest.mark.unit


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "openai-compatible",
        "name": "t",
        "api_key": "sk-x",
        "base_url": "https://example.com/v1",
        "model": "qwen3.8-max",
        "max_tokens": 100,
        "temperature": 0.7,
        "timeout_s": 60,
        "enable_search": False,
        "enable_thinking": False,
    }
    base.update(over)
    return base


def _et(model) -> bool:
    return bool((getattr(model, "extra_body", None) or {}).get("enable_thinking"))


# ---- layer 1: config → request body ----


def test_provider_timeout_s_bounds_request(monkeypatch):
    """P2 contract 7: the provider config's ``timeout_s`` (the value the
    settings UI shows) is the per-request chat timeout — not a dead field that
    only the verify probe read while chat stayed on the global constant."""
    from ginno_runtime.models import CHAT_TIMEOUT_S

    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(timeout_s=45)}
    )
    assert int(build_model("custom").request_timeout) == 45

    # malformed / missing value degrades to the module fallback
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(timeout_s="abc")}
    )
    assert int(build_model("custom").request_timeout) == int(CHAT_TIMEOUT_S)
    cfg = _cfg()
    cfg.pop("timeout_s")
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": cfg})
    assert int(build_model("custom").request_timeout) == int(CHAT_TIMEOUT_S)


def test_enable_thinking_sets_extra_body(monkeypatch):
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(enable_thinking=True)}
    )
    assert _et(build_model("custom")) is True


def test_no_thinking_by_default(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    assert _et(build_model("custom")) is False


def test_thinking_and_search_coexist_in_extra_body(monkeypatch):
    monkeypatch.setattr(
        prov_mod,
        "load_providers",
        lambda: {"custom": _cfg(enable_thinking=True, enable_search=True)},
    )
    eb = getattr(build_model("custom"), "extra_body", None) or {}
    assert eb.get("enable_thinking") is True
    assert eb.get("enable_search") is True


# ---- layer 2: streaming delta → additional_kwargs ----


def _raw_chunk(reasoning=None, content=None, finish=None) -> dict:
    """Shape of a model_dump()'d chat.completion.chunk as langchain-openai's
    _stream feeds it into _convert_chunk_to_generation_chunk."""
    delta: dict = {"role": "assistant"}
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if content is not None:
        delta["content"] = content
    return {"id": "c1", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _convert(model, chunk):
    return model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)


def test_subclass_restores_reasoning_content(monkeypatch):
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(enable_thinking=True)}
    )
    m = build_model("custom")
    gc = _convert(m, _raw_chunk(reasoning="let me think"))
    assert gc.message.additional_kwargs.get("reasoning_content") == "let me think"
    assert gc.message.content == ""


def test_subclass_keeps_plain_content(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    m = build_model("custom")
    gc = _convert(m, _raw_chunk(content="hi"))
    assert gc.message.content == "hi"
    assert "reasoning_content" not in gc.message.additional_kwargs


def test_reasoning_chunks_accumulate_on_merge(monkeypatch):
    # AIMessageChunk + AIMessageChunk merges additional_kwargs (strings
    # concatenate) — the persisted message ends up with the full reasoning.
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(enable_thinking=True)}
    )
    m = build_model("custom")
    a = _convert(m, _raw_chunk(reasoning="A ")).message
    b = _convert(m, _raw_chunk(reasoning="B")).message
    merged = a + b
    assert merged.additional_kwargs.get("reasoning_content") == "A B"


def test_empty_choices_chunk_does_not_crash(monkeypatch):
    # usage-only tail chunk (choices=[]) — base returns a chunk, we must not
    # blow up indexing into it.
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    m = build_model("custom")
    gc = _convert(m, {"id": "c1", "choices": [], "usage": None})
    assert gc is not None


def test_empty_reasoning_delta_survives(monkeypatch):
    # DeepSeek returns ``reasoning_content: ""`` on some tool-call turns and
    # demands the field back next request; a truthiness check would drop it.
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    m = build_model("custom")
    gc = _convert(m, _raw_chunk(reasoning=""))
    assert gc.message.additional_kwargs.get("reasoning_content") == ""


# ---- layer 3: history → request body (reasoning_content round-trip) ----


def _payload(monkeypatch, messages, **cfg_over):
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"custom": _cfg(**cfg_over)}
    )
    return build_model("custom")._get_request_payload(messages)


def _ai(reasoning=None, tool_calls=None, content=""):
    from langchain_core.messages import AIMessage

    extra = {} if reasoning is None else {"reasoning_content": reasoning}
    return AIMessage(
        content=content,
        additional_kwargs=extra,
        tool_calls=tool_calls or [],
    )


_TC = [{"name": "echo", "args": {"x": "1"}, "id": "call_1", "type": "tool_call"}]


def test_payload_replays_reasoning_from_additional_kwargs(monkeypatch):
    from langchain_core.messages import HumanMessage

    out = _payload(
        monkeypatch, [HumanMessage("hi"), _ai(reasoning="let me think", tool_calls=_TC)]
    )
    assert out["messages"][1]["reasoning_content"] == "let me think"


def test_payload_replays_anthropic_thinking_blocks(monkeypatch):
    """A session that started on an Anthropic-protocol thinking provider and
    was switched to DeepSeek mid-flight keeps its CoT as content blocks."""
    from langchain_core.messages import AIMessage, HumanMessage

    out = _payload(
        monkeypatch,
        [
            HumanMessage("hi"),
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "step 1"},
                    {"type": "text", "text": "answer"},
                ],
                tool_calls=_TC,
            ),
        ],
    )
    assert out["messages"][1]["reasoning_content"] == "step 1"


def test_payload_fills_empty_reasoning_for_tool_call_turns(monkeypatch):
    # Session switched over from a non-thinking provider: nothing to replay,
    # but DeepSeek 400s unless the field is present (empty string is accepted).
    from langchain_core.messages import HumanMessage

    out = _payload(monkeypatch, [HumanMessage("hi"), _ai(tool_calls=_TC)])
    assert out["messages"][1]["reasoning_content"] == ""


def test_payload_fills_reasoning_for_content_only_turns(monkeypatch):
    # DeepSeek 400s when the request's assistant messages omit the field —
    # including a synthetic content-only tail (hook-blocked result, notice).
    from langchain_core.messages import HumanMessage

    out = _payload(monkeypatch, [HumanMessage("hi"), _ai(content="just text")])
    assert out["messages"][1]["reasoning_content"] == ""


def test_official_openai_keeps_field_off(monkeypatch):
    # Not every endpoint accepts the non-standard field — the official OpenAI
    # protocol defaults to no injection even when history carries reasoning.
    from langchain_core.messages import HumanMessage

    out = _payload(
        monkeypatch,
        [HumanMessage("hi"), _ai(reasoning="x", tool_calls=_TC)],
        protocol="openai",
        base_url="",
    )
    assert "reasoning_content" not in out["messages"][1]


def test_config_override_disables_passthrough(monkeypatch):
    from langchain_core.messages import HumanMessage

    out = _payload(
        monkeypatch,
        [HumanMessage("hi"), _ai(tool_calls=_TC)],
        reasoning_passthrough=False,
    )
    assert "reasoning_content" not in out["messages"][1]
