"""E2E: web search → numbered sources → model cites [s1] → web telemetry.

Network-free: the engine call is monkeypatched (hermetic suite rule). The
model issues one web_search tool call, then answers with a citation block
(docs/citations-design.md §10 P1 acceptance).
"""

from __future__ import annotations

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from conftest import event_names
from ginno_runtime import paths
from ginno_runtime.knowledge import web_usage
from ginno_runtime.web import engines

pytestmark = pytest.mark.e2e

FINAL = (
    "LangGraph 的 checkpoint 支持增量快照[1]。\n\n"
    "<ginno_citations>\n"
    "web|s1|note=[增量快照机制的依据]\n"
    "web|https://never-searched.example/z|note=[编造的来源]\n"
    "</ginno_citations>"
)


class WebSearchModel(BaseChatModel):
    """Phase 1: call web_search; phase 2 (after ToolMessage): final answer."""

    _phase: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "websearch"

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self, messages):
        saw_tool = any(isinstance(m, ToolMessage) for m in messages)
        if not saw_tool:
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "web_search", "args": {"query": "langgraph checkpoint delta"}, "id": "call-1"}
                ],
            )
        return AIMessage(content=FINAL)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self._next(messages)
        if msg.tool_calls:
            # name-first chunk so the WS layer fires tool.start
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
            yield ChatGenerationChunk(message=AIMessageChunk(content=msg.content, id="webans"))


def test_web_search_citation_loop(create_session, ws_conv, client, monkeypatch):
    monkeypatch.setattr(
        engines,
        "ENGINES",
        {
            **engines.ENGINES,
            "duckduckgo": lambda q, cfg, t: [
                engines.SearchHit("Delta Checkpoints", "https://docs.example.com/checkpoints", "增量快照说明…"),
                engines.SearchHit("Other", "https://other.example.com/x", "无关…"),
            ],
        },
    )

    sid = create_session(WebSearchModel(), agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("帮我查一下 langgraph 的 checkpoint 机制")
        events = conv.recv_until("message.end", "error")
    names = event_names(events)
    assert names[-1] == "message.end"
    assert "tool.start" in names and "tool.end" in names

    # Web telemetry: verified citation credited to domain + engine; the
    # fabricated URL never lands in the domain table.
    data = json.loads(web_usage.web_usage_path().read_text())
    assert data["engines"]["duckduckgo"]["searches"] == 1
    assert data["engines"]["duckduckgo"]["hits_cited"] == 1
    doms = data.get("domains") or {}
    assert doms.get("docs.example.com", {}).get("cited") == 1
    assert "never-searched.example" not in doms

    # History: sources block carries both entries; the `web|s1` id is resolved
    # to its URL from the persisted web_search output (clickable 来源 card),
    # while the fabricated URL stays verbatim (unverified but parseable).
    hist = client.get(f"/api/sessions/{sid}/history").json()
    assistant = [m for m in hist["messages"] if m["role"] == "assistant"][-1]
    src = next((b for b in assistant["blocks"] if b.get("kind") == "sources"), None)
    assert src, assistant["blocks"]
    refs = [i["ref"] for i in src["items"]]
    assert "https://docs.example.com/checkpoints" in refs
    assert "s1" not in refs
    assert any("never-searched" in r for r in refs)
    # The web_search tool call itself renders as a tool block.
    assert any(b.get("kind") == "tool" and b.get("name") == "web_search" for b in assistant["blocks"])

    # Web usage endpoint exposes the aggregates.
    usage = client.get("/api/kb/wiki/web-usage").json()
    assert usage["ok"] is True
    assert usage["engines"][0]["engine"] == "duckduckgo"
    assert usage["top_domains"][0]["domain"] == "docs.example.com"

    # open-external validates the URL (public http/https only).
    assert client.post("/api/open-external", json={"url": "file:///etc/passwd"}).json()["ok"] is False
    assert client.post("/api/open-external", json={"url": ""}).json()["ok"] is False
