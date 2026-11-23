"""E2E 回归：流式事件因果顺序（2026-11-15 正文被工具行切断事故）。

根因：delta 合帧器（33ms 窗口）缓冲正文尾片时，messages 模式的 tool.start /
thinking.delta 裸发插队 → 前端先建工具块/思考块、后收文本尾片 → 正文被拦腰
切断，第二段以迟到的句号开头。updates 分支早有 flush 保序，messages 模式漏了。

Network-free：FakeModel 驱动真实 engine + WS 链路（hermetic suite rule）。
合帧窗口 monkeypatch 到 1s，排除定时器在断言前自动 flush 的干扰。
"""

from __future__ import annotations

import httpx
import pytest
from conftest import event_names, events_of
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult, ChatGeneration
from openai import AsyncOpenAI
from pydantic import PrivateAttr

from ginno_runtime import jsonx as json
from ginno_runtime import models
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


@pytest.mark.parametrize("protocol", ["openai-compatible", "openai-responses"])
def test_codex_reasoning_summary_reaches_websocket_once(
    protocol, client, create_session, ws_conv, monkeypatch,
):
    name = "codex/gpt-6.2-sol"
    cfg = {
        "enabled": True,
        "protocol": protocol,
        "api_key": "fixture-key",
        "base_url": "https://responses.example.test/v1",
        "default_model": name,
    }
    monkeypatch.setattr(models.prov_mod, "load_providers", lambda: {"fixture": cfg})
    model = models.build_model("fixture")
    wire_events = [
        {"type": "response.reasoning_summary_text.delta", "delta": text,
         "output_index": 0, "summary_index": 0, "item_id": "rs_test", "sequence_number": i}
        for i, text in enumerate(("Compare ", "choices."))
    ]
    wire_events.append({"type": "response.reasoning_summary_text.delta", "delta": "**Verify**",
                        "output_index": 0, "summary_index": 1, "item_id": "rs_test", "sequence_number": 2})
    wire_events.append({
        "type": "response.output_text.delta", "delta": "Choose B.",
        "output_index": 1, "content_index": 0, "item_id": "msg_test", "sequence_number": 3,
    })
    requests = []

    def provider(request):
        requests.append(request.url.path)
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in wire_events)
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    model.root_async_client = AsyncOpenAI(
        base_url=cfg["base_url"], api_key=cfg["api_key"], http_client=transport,
    )
    try:
        sid = create_session(model, title="Summary transport test")
        with ws_conv(sid) as conv:
            conv.invoke("Select an option.")
            events = conv.recv_until("message.end", "error")
    finally:
        client.portal.call(transport.aclose)

    assert requests == ["/v1/responses"]
    assert not events_of(events, "error")
    summaries = events_of(events, "reasoning.summary")
    assert summaries and not events_of(events, "thinking.delta")
    snapshot, = events_of(events, "message.end")[0]["reasoning_summaries"]
    parts = snapshot["parts"]
    assert [p["text"] for p in parts] == ["Compare choices.", "**Verify**"]
    received = {part["id"]: "" for part in parts}
    revision = 0
    for frame in summaries:
        assert "summary" not in frame
        delta = frame["summaryDelta"]
        assert delta["requestId"] == snapshot["requestId"]
        assert delta["baseRevision"] == revision
        revision = delta["revision"]
        for part in delta["parts"]:
            received[part["id"]] += part["text"]
    assert list(received.values()) == [part["text"] for part in parts]
    assert summaries[-1]["summaryDelta"]["receiving"] is False
    assert revision == snapshot["revision"]
    history = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert [b for m in history for b in m["blocks"] if b["kind"] == "reasoning_summary"] == [
        {"kind": "reasoning_summary", "requestId": snapshot["requestId"], "parts": parts},
    ]
    text_frames = events_of(events, "token.delta")
    assert "".join(e["content"] for e in text_frames) == "Choose B."
    assert {e["requestId"] for e in text_frames} == {snapshot["requestId"]}



def test_request_summaries_keep_order_after_a_tool_and_reconnect(client, create_session, ws_conv):
    import asyncio
    from threading import Event

    release = Event()

    def summary(text, index=0):
        return ChatGenerationChunk(message=AIMessageChunk(content=[{
            "type": "reasoning", "index": 0,
            "summary": [{"index": index, "type": "summary_text", "text": text}],
        }]))

    class PausedSummary(_OrderModel):
        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            if not any(isinstance(m, ToolMessage) for m in messages):
                yield summary("First request")
                async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
                    yield chunk
                return
            yield summary("Second request")
            assert await asyncio.to_thread(release.wait, 12)
            yield summary("Finish checking", 1)
            yield ChatGenerationChunk(message=AIMessageChunk(content="Answer"))

    sid = create_session(PausedSummary())
    with ws_conv(sid) as original:
        original.invoke("Compare")
        tool_events = original.recv_until("tool.end", "error")
        assert tool_events[-1]["event"] == "tool.end"
        first = events_of(tool_events, "reasoning.summary")[-1]["summaryDelta"]
        second = original.recv_until("reasoning.summary", "error")[-1]["summaryDelta"]
        assert not first["receiving"] and second["receiving"]
        assert first["requestId"] == tool_events[-1]["requestId"]
        assert first["requestId"] != second["requestId"]
        with ws_conv(sid) as reconnected:
            try:
                reconnected.send({"type": "turn_state"})
                probe = reconnected.recv_until("turn.state")[-1]
                assert probe["running"]
                first_snapshot, second_snapshot = probe["reasoning_summaries"]
                assert first_snapshot["revision"] == first["revision"]
                assert second_snapshot["revision"] == second["revision"]
                assert [p["text"] for p in first_snapshot["parts"]] == ["First request"]
                assert [p["text"] for p in second_snapshot["parts"]] == ["Second request"]
                reconnected.send({"type": "reasoning_summary_state", "requestId": second["requestId"]})
                recovery = reconnected.recv_until("reasoning.summary")[-1]
                assert recovery["recovery"] and recovery["summary"] == second_snapshot
                reconnected.send({"type": "turn_state", "include_reasoning_summaries": False})
                assert "reasoning_summaries" not in reconnected.recv_until("turn.state")[-1]
            finally:
                release.set()
            final = reconnected.recv_until("message.end", "error")[-1]
            assert final["reasoning_summaries"][0] == first_snapshot
            second_final = final["reasoning_summaries"][1]
            assert not second_final["receiving"]
            assert [p["text"] for p in second_final["parts"]] == ["Second request", "Finish checking"]

    history = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    blocks = next(m["blocks"] for m in history if m["role"] == "assistant")
    relevant = [b for b in blocks if b["kind"] in {"reasoning_summary", "text", "tool"}]
    assert [b["kind"] for b in relevant] == ["reasoning_summary", "text", "tool", "reasoning_summary", "text"]
    assert relevant[0]["requestId"] == first["requestId"]
    assert relevant[3]["requestId"] == second["requestId"]
