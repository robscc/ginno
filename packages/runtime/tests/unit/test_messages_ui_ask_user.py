"""History replay of ask_user tool calls → question blocks.

Regression suite for the 2026-09-20 turn aa49c475, where a real transcript got
TWO junk question cards because the model JSON-encoded the `options` argument
(`'["装进仓库", "装进全局"]'`) and:

* pydantic rejected it, so the tool returned a ToolInvocationError string, and
* the replay branch only filtered results starting with ``"[error]"`` — an
  error of any other shape was rendered as a card, with the string iterated
  one character per option.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from ginno_runtime.api.messages_ui import _messages_to_ui, _question_block

pytestmark = pytest.mark.unit


OPTIONS = ["装进当前仓库 .claude/skills", "装进 Ginno 全局"]


def _call(args: dict, tid: str = "call_q") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "ask_user", "args": args, "id": tid, "type": "tool_call"}],
    )


def _result(content: str, tid: str = "call_q") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tid, name="ask_user")


def _blocks(msgs) -> list[dict]:
    out = _messages_to_ui(msgs, None, None, project_slug="default", session_id="s1")
    return [b for m in out for b in (m.get("blocks") or [])]


def _questions(msgs) -> list[dict]:
    return [b for b in _blocks(msgs) if b.get("kind") == "question"]


# --------------------------------------------------------------------------- #
# the card exists, and only when it should
# --------------------------------------------------------------------------- #
def test_parked_call_replays_as_a_pending_card():
    msgs = [_call({"question": "装哪？", "options": OPTIONS, "header": "选择安装位置"})]
    q = _questions(msgs)
    assert len(q) == 1
    assert q[0]["id"] == "call_q"
    assert q[0]["status"] == "pending"
    assert q[0]["options"] == OPTIONS
    assert q[0]["header"] == "选择安装位置"
    assert q[0]["allowFreeText"] is True


def test_answered_call_replays_as_a_receipt():
    msgs = [
        _call({"question": "装哪？", "options": OPTIONS}),
        _result('{"ok": true, "skipped": false, "source": "option", '
                '"option_index": 1, "answer": "装进 Ginno 全局", "free_text": ""}'),
    ]
    q = _questions(msgs)
    assert len(q) == 1
    assert q[0]["status"] == "answered"
    assert q[0]["answer"] == "装进 Ginno 全局"
    assert q[0]["optionIndex"] == 1


def test_stopped_while_parked_replays_as_skipped():
    msgs = [_call({"question": "装哪？", "options": OPTIONS}), _result("(interrupted)")]
    assert _questions(msgs)[0]["status"] == "skipped"


def test_free_text_question_has_no_options():
    msgs = [_call({"question": "装哪？请直接说明"})]
    q = _questions(msgs)[0]
    assert q["options"] == [] and q["status"] == "pending"


# --------------------------------------------------------------------------- #
# the regression: a call that never showed a card is NOT a card
# --------------------------------------------------------------------------- #
def test_validation_failure_is_not_a_card():
    """THE turn-aa49c475 bug: the model stringified `options`, pydantic rejected
    it, and the error surfaced as ``ToolInvocationError(...)`` — which the old
    ``startswith("[error]")`` guard let through, rendering a card whose options
    were the characters of the JSON string."""
    err = (
        "Error: ToolInvocationError('Error invoking tool \\'ask_user\\' with "
        "kwargs {\\'question\\': \\'你要把 skill 装到哪个位置？\\', \\'options\\': "
        "\\'[\"装进仓库\", \"装进全局\"]\\'}')"
    )
    args = {"question": "装哪？", "options": '["装进当前仓库", "装进 Ginno 全局"]'}
    msgs = [_call(args), _result(err)]

    assert _questions(msgs) == [], "a failed call must not render a card"
    # …it replays as an ordinary tool bubble, matching the live stream
    tools = [b for b in _blocks(msgs) if b.get("kind") == "tool"]
    assert len(tools) == 1 and tools[0]["name"] == "ask_user"
    assert "ToolInvocationError" in tools[0]["content"]


def test_error_refusal_is_not_a_card():
    """An `[error]` refusal (ask budget spent / unattended turn) never parked —
    no card was shown live, so none may appear on replay."""
    msgs = [
        _call({"question": "q"}),
        _result("[error] 本轮已询问用户 3 次（上限 3）。请自行决定……"),
    ]
    assert _questions(msgs) == []


def test_unparseable_success_looking_result_is_not_a_card():
    msgs = [_call({"question": "q"}), _result("something went sideways")]
    assert _questions(msgs) == []


# --------------------------------------------------------------------------- #
# options are never iterated as a string
# --------------------------------------------------------------------------- #
def test_stringified_options_never_become_characters():
    """Even on the card path, a JSON-encoded options string must parse into real
    labels — one option per character was the visible symptom."""
    msgs = [
        _call({"question": "装哪？", "options": '["装进当前仓库", "装进 Ginno 全局"]'}),
        _result('{"ok": true, "skipped": true, "source": "skipped", "answer": ""}'),
    ]
    q = _questions(msgs)[0]
    assert q["options"] == ["装进当前仓库", "装进 Ginno 全局"]


def test_question_block_returns_none_for_errors_directly():
    assert _question_block("t", {"question": "q"}, "Error: boom") is None
    assert _question_block("t", {"question": "q"}, "[error] nope") is None
    blk = _question_block("t", {"question": "q", "options": ["a"]}, "")
    assert blk and blk["status"] == "pending"