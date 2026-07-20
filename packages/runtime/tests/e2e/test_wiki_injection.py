"""WebSocket E2E: wiki knowledge is retrieved and injected into the system prompt.

Uses a model that captures the system message it receives, so we can assert the
real graph (via the sidecar WebSocket) injected the relevant vault entry.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from conftest import event_names

pytestmark = pytest.mark.e2e


class CapturingModel(BaseChatModel):
    """Returns a fixed reply and records every system prompt it is given."""

    reply: str = "ok"
    _captured: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _capture(self, messages) -> None:
        for m in messages:
            if getattr(m, "type", "") == "system":
                c = m.content
                self._captured.append(c if isinstance(c, str) else "")
                break

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.reply, id="cap"))


def test_wiki_injected_into_system_prompt(create_session, ws_conv, kb_vault):
    model = CapturingModel(reply="权限节点会做 deny/ask/allow 匹配。")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("权限节点是怎么工作的？")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"

    assert model._captured, "model never received a system prompt"
    sys_prompt = model._captured[-1]
    assert "<injected_wiki>" in sys_prompt
    assert "LangGraph 权限节点" in sys_prompt          # the relevant entry
    assert "Obsidian Wiki 使用规范" in sys_prompt       # guidelines present
    assert "红烧肉" not in sys_prompt                  # irrelevant entry excluded


def test_no_injection_when_disabled(create_session, ws_conv):
    # no kb_vault fixture -> knowledge disabled
    model = CapturingModel(reply="hi")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("anything")
        conv.recv_until("message.end", "error")
    sys_prompt = model._captured[-1]
    assert "<injected_wiki>" not in sys_prompt
