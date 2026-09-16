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
