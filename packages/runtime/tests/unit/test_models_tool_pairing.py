"""Dangling ``tool_calls`` in history → synthetic tool replies on the wire.

A turn killed between its assistant step committing and the tools node
running (app quit / sidecar restart, 2026-10-23 session 4e5b9f89) leaves an
AIMessage whose tool_calls never ran. Every provider then 400s the whole
request, so every later turn of the session fails:

  OpenAI-compatible: "An assistant message with 'tool_calls' must be followed
  by tool messages responding to each 'tool_call_id'."

The repair is payload-only: ``_with_interrupted_tool_results`` inserts the
same ``(interrupted)`` ToolMessages the stop-path heal uses.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime import providers as prov_mod
from ginno_runtime.models import _with_interrupted_tool_results, build_model

pytestmark = pytest.mark.unit


def _ai(*ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "bash", "args": {}, "id": i, "type": "tool_call"} for i in ids
        ],
    )


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "openai-compatible",
        "name": "t",
        "api_key": "sk-x",
        "base_url": "https://example.com/v1",
        "model": "deepseek-flash",
        "max_tokens": 100,
        "temperature": 0.7,
        "timeout_s": 60,
        "enable_search": False,
        "enable_thinking": False,
    }
    base.update(over)
    return base


def test_tail_dangling_tool_calls_get_placeholders():
    out = _with_interrupted_tool_results([HumanMessage("go"), _ai("c1", "c2")])
    assert [m.type for m in out] == ["human", "ai", "tool", "tool"]
    assert [m.tool_call_id for m in out[2:]] == ["c1", "c2"]
    assert all(m.content == "(interrupted)" for m in out[2:])


def test_mid_history_dangling_placeholder_comes_before_next_message():
    # The real session shape: the killed step sits mid-history because later
    # user turns were appended after it.
    out = _with_interrupted_tool_results(
        [HumanMessage("go"), _ai("c1"), HumanMessage("继续"), HumanMessage("继续")]
    )
    assert [m.type for m in out] == ["human", "ai", "tool", "human", "human"]
    assert out[2].tool_call_id == "c1"


def test_partial_batch_only_unanswered_ids_are_filled():
    out = _with_interrupted_tool_results(
        [
            HumanMessage("go"),
            _ai("c1", "c2"),
            ToolMessage(content="ok", tool_call_id="c1"),
            HumanMessage("next"),
        ]
    )
    placeholders = [m for m in out if m.type == "tool"]
    assert [m.tool_call_id for m in placeholders] == ["c1", "c2"]
    assert placeholders[1].content == "(interrupted)"


def test_complete_batch_is_untouched():
    msgs = [
        HumanMessage("go"),
        _ai("c1"),
        ToolMessage(content="ok", tool_call_id="c1"),
        AIMessage(content="done"),
    ]
    out = _with_interrupted_tool_results(msgs)
    assert out == msgs


def test_chat_payload_carries_placeholder(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    payload = build_model("custom")._get_request_payload(
        [HumanMessage("go"), _ai("c1"), HumanMessage("继续")]
    )
    roles = [(m["role"], m.get("tool_call_id")) for m in payload["messages"]]
    assert roles == [
        ("user", None),
        ("assistant", None),
        ("tool", "c1"),
        ("user", None),
    ]


def test_anthropic_payload_carries_tool_result(monkeypatch):
    monkeypatch.setattr(
        prov_mod,
        "load_providers",
        lambda: {
            "custom": _cfg(protocol="anthropic", base_url="https://example.com")
        },
    )
    payload = build_model("custom")._get_request_payload(
        [HumanMessage("go"), _ai("c1"), HumanMessage("继续")]
    )
    blocks = [
        b
        for m in payload["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
    ]
    assert any(
        b.get("type") == "tool_result" and b.get("tool_use_id") == "c1"
        for b in blocks
    )


def test_tool_result_is_re_attached_to_its_own_call():
    """Two assistant tool-call messages committed back to back, with the first
    call's result landing after the second (2026-10-23 session 4e5b9f89) —
    providers treat the call as unanswered unless the result sits right after
    its assistant."""
    out = _with_interrupted_tool_results(
        [
            HumanMessage("go"),
            _ai("X"),
            _ai("Y"),
            ToolMessage(content="real-X", tool_call_id="X"),
        ]
    )
    assert [(m.type, getattr(m, "tool_call_id", None)) for m in out] == [
        ("human", None),
        ("ai", None),
        ("tool", "X"),
        ("ai", None),
        ("tool", "Y"),
    ]
    assert out[2].content == "real-X"  # the real answer, not a placeholder
    assert out[4].content == "(interrupted)"


def test_orphan_tool_message_is_dropped():
    out = _with_interrupted_tool_results(
        [HumanMessage("go"), ToolMessage(content="stale", tool_call_id="ghost")]
    )
    assert [m.type for m in out] == ["human"]


def test_invalid_tool_calls_also_get_answers():
    # _convert_message_to_dict sends invalid_tool_calls too, so a placeholder
    # is required for those ids as well.
    msg = AIMessage(
        content="",
        invalid_tool_calls=[
            {"name": "bash", "args": "{bad json", "id": "bad1", "error": "parse"}
        ],
    )
    out = _with_interrupted_tool_results([HumanMessage("go"), msg])
    assert [m.type for m in out] == ["human", "ai", "tool"]
    assert out[2].tool_call_id == "bad1"
