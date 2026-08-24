"""API test for P6: POST /workflows/summarize-from-session distills a session's
conversation (read from the checkpointer) into a validated workflow DSL draft,
without saving it."""

from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.api


def _seed_session(slug: str, sid: str) -> None:
    """Write a session index entry + one checkpoint so the summarizer sees it."""
    from ginno_runtime import server

    server._session_meta_upsert(slug, {"id": sid, "title": "synth", "agent_id": "dev"})
    cp = FileCheckpointer(slug)
    state = {
        "messages": [
            HumanMessage(content="list the open PRs and review each"),
            AIMessage(
                content="sure",
                tool_calls=[{"name": "list_prs", "args": {}, "id": "t1", "type": "tool_call"}],
            ),
            HumanMessage(content="thanks, now summarise"),
            AIMessage(content="here is the summary"),
        ],
        "workspace": "/tmp",
        "project_slug": slug,
        "agent_id": "dev",
        "active_skills": [],
        "pending_tool_calls": [],
    }
    checkpoint = {"id": str(uuid.uuid4()), "channel_values": state, "pending_sends": []}
    cp.put({"configurable": {"thread_id": sid}}, checkpoint, {}, {})


def _await_case(client, synthesis_id: str) -> dict:
    """Deterministically await the background synthesis task; return the case."""
    aw = client.post(f"/api/synthesis/cases/{synthesis_id}/_await").json()
    assert aw["ok"] is True and aw.get("error") is None, aw
    return aw["case"]


def test_summarize_returns_valid_dsl_draft(client, monkeypatch):
    from ginno_runtime import server

    sid = "sess-synth-1"
    _seed_session("default", sid)

    dsl_json = json.dumps(
        {
            "name": "PR Review",
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "research", "goal": "list PRs"},
                {"id": "s2", "type": "step", "agent": "dev", "goal": "review each"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        }
    )
    sm = ScriptedChatModel(scripts=[script(text=dsl_json)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: sm)

    # Endpoint returns immediately with the synthesis_id; the DSL arrives once
    # the background task finishes (observed via _await / WS / polling).
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body.get("synthesis_id")
    assert body.get("status") == "started"

    case = _await_case(client, body["synthesis_id"])
    assert case["output"]["status"] == "ok"
    assert case["output"]["dsl"]["entry"] == "s1"
    assert len(case["output"]["dsl"]["nodes"]) == 2
    assert case["input"]["session_id"] == sid
    # a draft is NOT persisted as a workflow definition
    assert all(w.get("name") != "PR Review" for w in client.get("/api/workflows").json())


def test_summarize_rejects_invalid_model_output(client, monkeypatch):
    from ginno_runtime import server

    sid = "sess-synth-2"
    _seed_session("default", sid)
    sm = ScriptedChatModel(scripts=[script(text="sorry, I cannot produce a DSL right now")])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: sm)
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    assert r.status_code == 200
    syn_id = r.json()["synthesis_id"]
    case = _await_case(client, syn_id)
    assert case["output"]["status"] == "failed"
    assert case["output"]["fail_stage"] == "format.not_json"
    # raw model output is preserved per-attempt in the case record
    assert case["attempts"] and case["attempts"][0]["raw"]


def test_summarize_404_for_unknown_session(client):
    assert client.post("/api/workflows/summarize-from-session", json={"session_id": "nope"}).status_code == 404


def test_summarize_handles_thinking_block_content(client, monkeypatch):
    """Extended-thinking hub models return content as a block list
    ('', {'thinking': ...}, '<json>') — the text parts must still be parsed.
    Regression: str(list) corrupted the payload → ok:false (2026-08-09)."""
    sid = "sess-synth-3"
    _seed_session("default", sid)

    dsl_json = json.dumps(
        {
            "name": "Thinking WF",
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
            "edges": [],
        }
    )

    class _ThinkingModel:
        async def ainvoke(self, *_a, **_k):
            return AIMessage(content=["", {"thinking": "reasoning..."}, dsl_json])

    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: _ThinkingModel())
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    assert r.status_code == 200, r.text
    syn_id = r.json()["synthesis_id"]
    case = _await_case(client, syn_id)
    assert case["output"]["status"] == "ok"
    assert case["output"]["dsl"]["name"] == "Thinking WF"
