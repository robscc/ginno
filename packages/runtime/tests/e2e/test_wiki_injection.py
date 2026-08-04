"""WebSocket E2E: wiki knowledge is retrieved and injected as turn context.

Plan B1 moved per-turn volatile content (wiki retrieval) out of the stable
system prompt into a turn-context message appended before the user message.
These tests capture every message the model receives and assert the new
contract: wiki rides the turn-context message; the system prompt carries the
stable WorldState layer (<environment> etc.) and nothing query-dependent.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from conftest import event_names
from ginno_runtime.world_state import TURN_CONTEXT_PREFIX

pytestmark = pytest.mark.e2e


class CapturingModel(BaseChatModel):
    """Returns a fixed reply; records system prompts and human messages."""

    reply: str = "ok"
    _captured: list = PrivateAttr(default_factory=list)  # system prompts
    _humans: list = PrivateAttr(default_factory=list)  # human message contents

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _capture(self, messages) -> None:
        for m in messages:
            t = getattr(m, "type", "")
            c = getattr(m, "content", "")
            if t == "system":
                self._captured.append(c if isinstance(c, str) else "")
            elif t == "human":
                self._humans.append(c if isinstance(c, str) else str(c))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.reply, id="cap"))


def _turn_context(model) -> str:
    for h in reversed(model._humans):
        if isinstance(h, str) and h.startswith(TURN_CONTEXT_PREFIX):
            return h
    return ""


def test_wiki_injected_into_turn_context(create_session, ws_conv, kb_vault):
    model = CapturingModel(reply="权限节点会做 deny/ask/allow 匹配。")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("权限节点是怎么工作的？")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"

    turn_ctx = _turn_context(model)
    assert turn_ctx, "model never received a turn-context message"
    assert "<injected_wiki>" in turn_ctx
    assert "LangGraph 权限节点" in turn_ctx            # the relevant entry
    assert "Obsidian Wiki 使用规范" in turn_ctx         # guidelines present
    assert "红烧肉" not in turn_ctx                    # irrelevant entry excluded

    # B2: the stable system layer carries WorldState sections and NO wiki
    assert model._captured, "model never received a system prompt"
    sys_prompt = model._captured[-1]
    assert "<environment>" in sys_prompt
    assert "<injected_wiki>" not in sys_prompt


def test_no_injection_when_disabled(create_session, ws_conv):
    # no kb_vault fixture -> knowledge disabled
    model = CapturingModel(reply="hi")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("anything")
        conv.recv_until("message.end", "error")
    assert "<injected_wiki>" not in model._captured[-1]
    assert "<injected_wiki>" not in _turn_context(model)
