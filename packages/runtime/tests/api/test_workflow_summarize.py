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


# ---- 方案B 阶段4: message-range selection + import-into-existing confirm ----


_DSL_JSON = json.dumps(
    {
        "name": "Range WF",
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
        "edges": [],
    }
)


def _summarize(client, monkeypatch, body: dict) -> dict:
    """Start a synthesis with a scripted model; return the awaited case."""
    sm = ScriptedChatModel(scripts=[script(text=_DSL_JSON)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: sm)
    r = client.post("/api/workflows/summarize-from-session", json=body)
    assert r.status_code == 200, r.text
    return _await_case(client, r.json()["synthesis_id"])


def test_summarize_trace_rows_index_checkpoint_messages(client):
    """The trace picker's rows carry the EXACT indices the range param slices."""
    sid = "sess-synth-4"
    _seed_session("default", sid)
    r = client.get(f"/api/workflows/summarize-trace/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["count"] == 4
    assert [row["i"] for row in body["rows"]] == [0, 1, 2, 3]
    roles = [row["role"] for row in body["rows"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert body["rows"][1]["tools"] == ["list_prs"]
    assert body["rows"][2]["text"].startswith("thanks")
    assert client.get("/api/workflows/summarize-trace/nope").status_code == 404


def test_summarize_range_slices_the_trace(client, monkeypatch):
    """range {start, end} (inclusive) limits what the synthesizer sees; the
    resolved window is recorded on the case input."""
    sid = "sess-synth-5"
    _seed_session("default", sid)
    case = _summarize(client, monkeypatch, {"session_id": sid, "range": {"start": 1, "end": 2}})
    trace = case["input"]["trace"]
    assert "list the open PRs" not in trace  # message 0 excluded
    assert "here is the summary" not in trace  # message 3 excluded
    assert "now summarise" in trace  # message 2 included
    assert case["input"]["msg_range"] == {"start": 1, "end": 2}
    # the synth model only ever saw the selected slice
    assert "now summarise" in trace


def test_summarize_range_clamps_to_session_bounds(client, monkeypatch):
    """start < 0 clamps to 0, end beyond the last index clamps to it."""
    sid = "sess-synth-6"
    _seed_session("default", sid)
    case = _summarize(client, monkeypatch, {"session_id": sid, "range": {"start": -5, "end": 99}})
    trace = case["input"]["trace"]
    assert "list the open PRs" in trace and "here is the summary" in trace
    assert case["input"]["msg_range"] == {"start": 0, "end": 3}


def test_summarize_last_n_wins_over_range(client, monkeypatch):
    """Back-compat: when both are given, last_n (tail window) wins."""
    sid = "sess-synth-7"
    _seed_session("default", sid)
    case = _summarize(
        client, monkeypatch,
        {"session_id": sid, "last_n": 2, "range": {"start": 0, "end": 1}},
    )
    trace = case["input"]["trace"]
    assert "thanks, now summarise" in trace  # message 2 (tail of 2 starts here)
    assert "list the open PRs" not in trace  # message 0 cut by the tail window
    assert case["input"]["msg_range"] == {"last_n": 2}  # tail-window descriptor
    assert case["input"]["last_n"] == 2


def test_summarize_range_past_the_end_rejected(client, monkeypatch):
    """A range that clamps to an empty selection is a 400, not a silent full run."""
    sid = "sess-synth-8"
    _seed_session("default", sid)
    sm = ScriptedChatModel(scripts=[script(text=_DSL_JSON)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: sm)
    r = client.post(
        "/api/workflows/summarize-from-session",
        json={"session_id": sid, "range": {"start": 10, "end": 20}},
    )
    assert r.status_code == 400, r.text
    assert "range selects no messages" in r.json()["detail"]


def _new_wf(client, name: str) -> dict:
    r = client.post(
        "/api/workflows",
        json={
            "name": name,
            "dsl": {
                "name": name,
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "old"}],
                "edges": [],
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["workflow"]


def test_confirm_into_existing_workflow_creates_new_version(client, monkeypatch):
    """POST /api/workflows with workflow_id applies the draft as v(N+1) of THAT
    recipe only; the synthesis case is backfilled as adopted."""
    sid = "sess-synth-9"
    _seed_session("default", sid)
    wf_a = _new_wf(client, "Target WF")
    wf_b = _new_wf(client, "Bystander WF")

    sm = ScriptedChatModel(scripts=[script(text=_DSL_JSON)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: sm)
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    syn_id = r.json()["synthesis_id"]
    _await_case(client, syn_id)

    draft = {
        "name": "Target WF",
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "agent": "dev", "goal": "new goal"},
            {"id": "s2", "type": "step", "agent": "dev", "goal": "second"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
    r2 = client.post(
        "/api/workflows", json={"dsl": draft, "synthesis_id": syn_id, "workflow_id": wf_a["id"]}
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["ok"] is True and body["workflow"]["id"] == wf_a["id"]
    assert body["workflow"]["version"] == 2
    assert len(body["workflow"]["dsl"]["nodes"]) == 2
    # no NEW recipe appeared; the bystander is untouched
    names = [w["name"] for w in client.get("/api/workflows").json()]
    assert names.count("Target WF") == 1
    assert [w["version"] for w in client.get("/api/workflows").json() if w["id"] == wf_b["id"]] == [1]
    # adoption backfill recorded the existing workflow as the outcome
    case = client.get(f"/api/synthesis/cases/{syn_id}").json()["case"]
    assert case["outcome"]["created"] is True
    assert case["outcome"]["workflow_id"] == wf_a["id"]


def test_confirm_into_unknown_workflow_404(client):
    r = client.post(
        "/api/workflows",
        json={
            "dsl": {"name": "x", "entry": "s1", "nodes": [], "edges": []},
            "workflow_id": "does-not-exist",
        },
    )
    assert r.status_code == 404, r.text


def test_confirm_into_existing_requires_dsl(client):
    wf = _new_wf(client, "No DSL WF")
    r = client.post("/api/workflows", json={"workflow_id": wf["id"]})
    assert r.status_code == 400, r.text
