"""E2E 回归：流式事件因果顺序（2026-11-15 正文被工具行切断事故）。

根因：delta 合帧器（33ms 窗口）缓冲正文尾片时，messages 模式的 tool.start /
thinking.delta 裸发插队 → 前端先建工具块/思考块、后收文本尾片 → 正文被拦腰
切断，第二段以迟到的句号开头。updates 分支早有 flush 保序，messages 模式漏了。

Network-free：FakeModel 驱动真实 engine + WS 链路（hermetic suite rule）。
合帧窗口 monkeypatch 到 1s，排除定时器在断言前自动 flush 的干扰。
"""

from __future__ import annotations

import pytest
from conftest import event_names
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult, ChatGeneration
from pydantic import PrivateAttr

from ginno_runtime import jsonx as json
from ginno_runtime.api.stream import delta_coalesce

pytestmark = pytest.mark.e2e


class _OrderModel(BaseChatModel):
    """Phase 1：正文两片 + 一个 tool_call；Phase 2：收尾正文。
    片间无 await 间隔——尾片必然还在合帧缓冲里，tool.start 若裸发必插队。"""

    _phase: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "order-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self, messages):
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content="完成")
        return AIMessage(
            content="",
            tool_calls=[{"name": "glob_files", "args": {"pattern": "*.md"}, "id": "call-o1"}],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self._next(messages)
        if msg.tool_calls:
            # 正文首片（即时发）+ 尾片（进缓冲）——事故里那个被切断的句号
            yield ChatGenerationChunk(message=AIMessageChunk(content="正文前半"))
            yield ChatGenerationChunk(message=AIMessageChunk(content="。"))
            tc = msg.tool_calls[0]
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": tc["name"], "args": "", "id": tc["id"], "index": 0}
                    ],
                )
            )
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {"name": None, "args": json.dumps(tc["args"]), "id": None, "index": 0}
                    ],
                )
            )
        else:
            yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content))


class _ThinkingOrderModel(BaseChatModel):
    """正文两片缓冲中 → thinking 块到达。旧实现 thinking 裸发插队到文本尾片前。"""

    @property
    def _llm_type(self) -> str:
        return "thinking-order-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        yield ChatGenerationChunk(message=AIMessageChunk(content="正文"))
        yield ChatGenerationChunk(message=AIMessageChunk(content="尾片"))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content=[{"type": "thinking", "thinking": "思考内容", "index": 0}]
            )
        )


@pytest.fixture(autouse=True)
def _long_coalesce_window(monkeypatch):
    """1s 窗口：测试断言期间定时器不会自动 flush，插队缺陷必然暴露。"""
    monkeypatch.setattr(delta_coalesce, "coalesce_window_s", lambda: 1.0)


def test_text_tail_lands_before_tool_start(create_session, ws_conv):
    sid = create_session(_OrderModel(), agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("跑个工具")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert names[-1] == "message.end", names
    assert "tool.start" in names
    ti = names.index("tool.start")
    # tool.start 之前的 token.delta 必须已含完整正文（尾片「。」不得迟到）
    before = "".join(
        e.get("content", "") for e in events[:ti] if e.get("event") == "token.delta"
    )
    assert before == "正文前半。", (
        f"正文尾片被 tool.start 插队切断: tool.start 前只收到 {before!r}"
    )
    # tool.start 之后不得再出现属于第一段的 token.delta（尾片迟到的直接证据）
    after_first = [
        e.get("content", "")
        for e in events[ti + 1 :]
        if e.get("event") == "token.delta"
    ]
    assert "。" not in after_first, f"句号尾片迟到至 tool.start 之后: {after_first}"


def test_thinking_does_not_overtake_buffered_text(create_session, ws_conv):
    sid = create_session(_ThinkingOrderModel(), agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("思考一下")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert names[-1] == "message.end", names
    assert "thinking.delta" in names
    ki = names.index("thinking.delta")
    before = "".join(
        e.get("content", "") for e in events[:ki] if e.get("event") == "token.delta"
    )
    assert before == "正文尾片", (
        f"thinking 插队到缓冲文本前: thinking.delta 前只收到 {before!r}"
    )