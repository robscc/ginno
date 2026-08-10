"""E2E: the citation loop — inject → model cites → validate → ledger → UI.

Uses the kb_vault fixture + a scripted capturing model whose answer carries a
``<ginno_citations>`` block with one verified and one hallucinated wiki entry
(docs/citations-design.md §10 P0 acceptance).
"""

from __future__ import annotations

import glob
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from conftest import event_names
from ginno_runtime import paths

pytestmark = pytest.mark.e2e

REPLY = (
    "权限节点按 deny→ask→allow 的顺序匹配（见 [[LangGraph 权限节点]]）。\n\n"
    "<ginno_citations>\n"
    "wiki|Ginno/Wiki/concepts/permission.md|note=[匹配顺序的依据]\n"
    "wiki|Ginno/Wiki/ghost.md|note=[模型编造的页]\n"
    "</ginno_citations>"
)


class CitingModel(BaseChatModel):
    reply: str = REPLY
    _humans: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "citing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for m in messages:
            if getattr(m, "type", "") == "human":
                self._humans.append(getattr(m, "content", ""))
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.reply, id="cite"))


def _ledger() -> dict:
    p = paths.knowledge_dir() / "usage.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_citation_loop_end_to_end(create_session, ws_conv, kb_vault, client):
    model = CitingModel()
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("权限节点是怎么工作的？")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"

    # The injected page reached the model WITH the citation contract.
    turn_ctx = [h for h in model._humans if isinstance(h, str) and "injected_wiki" in h]
    assert turn_ctx and "引用规范" in turn_ctx[-1]

    # Ledger: verified citation counted, hallucination lands in _invalid.
    data = _ledger()
    page = data["Ginno/Wiki/concepts/permission.md"]
    assert page["injected"] == 1
    assert page["cited"] == 1
    assert page["last_session"] == sid
    inv = data.get("_invalid") or {}
    assert inv.get("cited") == 1
    assert any("ghost" in s for s in inv.get("samples") or [])
    # The hallucinated page itself must not gain a ledger row.
    assert "Ginno/Wiki/ghost.md" not in data

    # History: raw block stripped, rendered as a sources block instead.
    hist = client.get(f"/api/sessions/{sid}/history").json()
    assistant = [m for m in hist["messages"] if m["role"] == "assistant"]
    assert assistant, hist
    blocks = assistant[-1]["blocks"]
    kinds = [b.get("kind") for b in blocks]
    assert "sources" in kinds
    src = next(b for b in blocks if b.get("kind") == "sources")
    refs = [i["ref"] for i in src["items"]]
    assert "Ginno/Wiki/concepts/permission.md" in refs
    assert "Ginno/Wiki/ghost.md" in refs  # parsed even if unverified
    for b in blocks:
        if b.get("kind") == "text":
            assert "ginno_citations" not in b["text"]

    # Memory pool capture must not carry the block (or its tags).
    pool_files = glob.glob(str(paths.memory_pool_dir() / "*.jsonl"))
    assert pool_files, "assistant turn was not captured into the memory pool"
    captured = "".join(open(f, encoding="utf-8").read() for f in pool_files)
    assert "ginno_citations" not in captured
    assert "匹配顺序的依据" not in captured  # block incl. notes is fully stripped
    assert "deny→ask→allow" in captured  # the prose itself is still captured

    # Usage endpoint exposes the ledger.
    usage = client.get("/api/kb/wiki/usage").json()
    assert usage["ok"] is True
    row = next(r for r in usage["rows"] if r["path"] == "Ginno/Wiki/concepts/permission.md")
    assert row["cited"] == 1 and row["injected"] == 1 and row["rate"] == 1.0
    # Stats carry citation aggregates.
    stats = client.get("/api/kb/wiki/stats").json()
    assert stats["citations"]["total_cited"] == 1
    assert stats["citations"]["invalid_cited"] == 1


def test_answer_without_citations_is_a_noop(create_session, ws_conv, kb_vault):
    class PlainModel(CitingModel):
        reply: str = "权限节点做匹配。"  # no block at all

    sid = create_session(PlainModel(), agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("权限节点是怎么工作的？")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"
    data = _ledger()
    assert data["Ginno/Wiki/concepts/permission.md"]["injected"] == 1
    assert data["Ginno/Wiki/concepts/permission.md"].get("cited", 0) == 0
