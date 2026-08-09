"""Contract tests for the workflow-ux-redesign features (E2E via the HTTP API).

Covers:
1. P1 pending_interrupt stamping on paused runs + human resume payloads
   (frontend sends ``{"answer": ...}`` / ``{"answer": null, "skip": true}``).
2. P2 retry_from_checkpoint: failed run → new run continuing from the copied
   checkpoint (completed prefix must NOT re-execute) + its 409 guard rails.
3. cleanup ``statuses`` filter (only the requested terminal statuses deleted).
4. summarize-from-session: ``last_n`` trace limiting + the bounded (3-attempt)
   self-correction retry loop.

NOTE (verified actual behavior, see report):
* ``pending_interrupt`` is stamped with ``node_id`` (not ``node`` — the
  frontend type/RunBlocks read ``.node``, so the answer card never resolves a
  node title from the stamped payload).
* The copied checkpoint record still declares the SOURCE run id as its
  ``session_id`` and FileCheckpointer._write keys on that field, so the
  continuation's new checkpoints land back in the SOURCE run's file — the
  continuation EXECUTES the suffix but terminates as "paused" instead of
  "done" (its terminal check reads the frozen checkpoint copy).
"""

from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime import server
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _human_wf():
    return {
        "name": "HumanGate",
        "dsl": {
            "entry": "h",
            "nodes": [
                {"id": "h", "type": "human", "question": "approve?"},
                {"id": "s", "type": "llm", "prompt": "continue after the gate"},
            ],
            "edges": [{"from": "h", "to": "s"}],
        },
    }


def _llm_chain_wf():
    """n1(llm) -> n2(llm) -> n3(pass): prefix that can fail mid-chain."""
    return {
        "name": "Chain3",
        "dsl": {
            "entry": "n1",
            "nodes": [
                {"id": "n1", "type": "llm", "prompt": "first", "output": "a"},
                {"id": "n2", "type": "llm", "prompt": "second", "output": "b"},
                {"id": "n3", "type": "pass"},
            ],
            "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"}],
        },
    }


class _SeqModel:
    """Plain-object fake model: replays replies, optionally raises from call N.

    Tracks ``calls`` (number of ainvoke calls) so tests can prove which nodes
    actually executed (each llm node invokes the model exactly once)."""

    def __init__(self, replies=(), fail_on: int | None = None, fail_msg: str = "boom"):
        self.replies = list(replies)
        self.fail_on = fail_on  # 1-based index of the call that raises
        self.fail_msg = fail_msg
        self.calls = 0

    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, messages, *a, **k):
        self.calls += 1
        if self.fail_on is not None and self.calls >= self.fail_on:
            raise RuntimeError(self.fail_msg)
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="")


def _patch_build_model(monkeypatch, queue: list):
    """Each run (each _wf_build_deps call) pops the next model from the queue."""
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: queue.pop(0)
    )


# --------------------------------------------------------------------------- #
# 1. pending_interrupt stamping + resume payloads
# --------------------------------------------------------------------------- #
def test_pending_interrupt_stamped_on_pause(client, monkeypatch):
    model = ScriptedChatModel(scripts=[script(text="after the gate")])
    _patch_build_model(monkeypatch, [model])
    wf = store.create_def(_human_wf())

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw

    run = client.get(f"/api/workflow_runs/{run_id}").json()["run"]
    # The stamped payload mirrors the interrupt EVENT fields (node_id/question)
    # with kind flipped to "human" (api/workflows.py _drive_run_events). Since
    # the ts-fidelity change (§2.4) the event also carries a `ts`; the payload
    # copies it, so assert the semantic fields and tolerate the timestamp.
    pi = run["pending_interrupt"]
    assert pi["kind"] == "human"
    assert pi["node_id"] == "h"
    assert pi["question"] == "approve?"

    # the persisted interrupt event carries the same shape (kind=interrupt)
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    intr = next(e for e in evs if e["kind"] == "interrupt")
    assert intr["node_id"] == "h"
    assert intr["question"] == "approve?"


def test_resume_with_frontend_answer_payload_completes_run(client, monkeypatch):
    """Frontend 确认继续 sends ``{"answer": "..."}`` — HumanNode takes the dict
    verbatim as its ``__output__`` (only a nested context_patch would merge)."""
    model = ScriptedChatModel(scripts=[script(text="after the gate")])
    # NOTE: resume re-builds deps (_wf_build_deps) in its own driver, so it
    # pops a second model from the patched build_model.
    _patch_build_model(monkeypatch, [model, model])
    wf = store.create_def(_human_wf())

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # paused
    assert store.get_run(run_id)["status"] == "paused"

    r = client.post(f"/api/workflow_runs/{run_id}/resume", json={"answer": "yes"})
    assert r.status_code == 200 and r.json()["status"] == "resuming"
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw

    run = client.get(f"/api/workflow_runs/{run_id}").json()["run"]
    assert run["pending_interrupt"] is None  # cleared on resume/done
    kinds = [e["kind"] for e in client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]]
    assert "resume" in kinds and kinds[-1] == "done"
    # the node after the gate actually executed (consumed the scripted turn)
    assert model._i == 1
    # FIXED (was a quirk): HumanNode emits no node_enter/node_exit, so the API
    # driver now stamps the step "running" on the interrupt event and "done"
    # on resume — a DONE run shows [("h","done"), ("s","done")].
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps == {"h": "done", "s": "done"}


def test_resume_with_frontend_skip_payload_completes_run(client, monkeypatch):
    """Frontend 跳过 sends ``{"answer": null, "skip": true}`` — also just a dict
    to HumanNode, so it resumes cleanly."""
    model = ScriptedChatModel(scripts=[script(text="after the gate")])
    _patch_build_model(monkeypatch, [model, model])
    wf = store.create_def(_human_wf())

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # paused

    r = client.post(f"/api/workflow_runs/{run_id}/resume", json={"answer": None, "skip": True})
    assert r.status_code == 200
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    assert client.get(f"/api/workflow_runs/{run_id}").json()["run"]["pending_interrupt"] is None


def test_resume_rejects_non_paused_run(client, monkeypatch):
    model = ScriptedChatModel(scripts=[script(text="x")])
    _patch_build_model(monkeypatch, [model])
    wf = store.create_def(
        {
            "name": "NoGate",
            "dsl": {
                "entry": "s",
                "nodes": [{"id": "s", "type": "llm", "prompt": "go"}],
                "edges": [],
            },
        }
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # done
    assert client.post(f"/api/workflow_runs/{run_id}/resume", json={"answer": "x"}).status_code == 409


# --------------------------------------------------------------------------- #
# 2. retry_from_checkpoint
# --------------------------------------------------------------------------- #
def test_retry_from_checkpoint_continues_after_failed_node(client, monkeypatch):
    orig = _SeqModel(replies=[AIMessage(content="n1 done")], fail_on=2, fail_msg="boom-n2")
    cont = _SeqModel(replies=[AIMessage(content="n2 done")])
    _patch_build_model(monkeypatch, [orig, cont])
    wf = store.create_def(_llm_chain_wf())

    # --- original run fails at n2 ------------------------------------------- #
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    assert "boom-n2" in (run.get("error") or "")
    assert (run.get("error_detail") or {}).get("node_id") == "n2"
    ckpt = server._run_checkpoint_path(run_id)
    assert ckpt.exists(), "failed run must leave a checkpoint behind"
    ckpt_count_after_failure = len(json.loads(ckpt.read_text())["checkpoints"])

    # --- retry_from_checkpoint ---------------------------------------------- #
    r = client.post(f"/api/workflow_runs/{run_id}/retry_from_checkpoint")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["source_run_id"] == run_id
    new = body["run"]
    assert new["retried_from"] == run_id
    assert store.get_run(run_id)["retry_run_id"] == new["id"]
    new_ckpt = server._run_checkpoint_path(new["id"])
    assert new_ckpt.exists(), "checkpoint file must be copied for the new run"

    # --- the continuation executes the failed node + suffix ----------------- #
    aw2 = client.post(f"/api/workflow_runs/{new['id']}/_await").json()

    # Prefix skipped: the new run's events show node_enter for n2/n3 ONLY —
    # n1 was NOT re-executed (astream(None) resumed from the checkpoint).
    evs = client.get(f"/api/workflow_runs/{new['id']}/events").json()["events"]
    enters = [e.get("node_id") for e in evs if e["kind"] == "node_enter"]
    assert "n1" not in enters, f"completed prefix re-executed: {enters}"
    assert enters == ["n2", "n3"]
    # The continuation model was invoked exactly once (n2 only; n3 is a pass
    # node). A from-the-start re-run would have invoked it twice.
    assert cont.calls == 1, f"expected 1 model call (resume), saw {cont.calls}"

    # FIXED (was a bug): the copied checkpoint record's embedded session_id is
    # retagged to the NEW run id before the continuation starts, so resumed
    # checkpoints append to the new run's file and the run reaches "done".
    assert aw2["run"]["status"] == "done", aw2
    old_rec = json.loads(ckpt.read_text())
    new_rec = json.loads(new_ckpt.read_text())
    assert new_rec["session_id"] == new["id"]  # retagged on clone
    assert len(old_rec["checkpoints"]) == ckpt_count_after_failure  # source untouched
    assert len(new_rec["checkpoints"]) > ckpt_count_after_failure


def test_retry_from_checkpoint_409_without_node_attribution(client, monkeypatch):
    """Driver-level failure (agent fork) has error_detail.node_id == None."""
    _patch_build_model(monkeypatch, [ScriptedChatModel(scripts=[script(text="x")])])
    wf = store.create_def(
        {
            "name": "Ghost",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "ghost", "goal": "x"}],
                "edges": [],
            },
        }
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "failed"
    assert (aw["run"].get("error_detail") or {}).get("node_id") is None
    r = client.post(f"/api/workflow_runs/{run_id}/retry_from_checkpoint")
    assert r.status_code == 409


def test_retry_from_checkpoint_409_for_non_failed(client, monkeypatch):
    _patch_build_model(monkeypatch, [ScriptedChatModel(scripts=[script(text="x")])])
    wf = store.create_def(
        {
            "name": "Ok",
            "dsl": {
                "entry": "s",
                "nodes": [{"id": "s", "type": "llm", "prompt": "go"}],
                "edges": [],
            },
        }
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # done
    assert client.post(f"/api/workflow_runs/{run_id}/retry_from_checkpoint").status_code == 409
    assert client.post("/api/workflow_runs/nope/retry_from_checkpoint").status_code == 404


def test_retry_from_checkpoint_409_when_checkpoint_missing(client):
    wf = store.create_def(_llm_chain_wf())
    run = store.create_run(wf)
    server._set_run_status(
        run["id"], "failed", error="RuntimeError: x",
        error_detail={"node_id": "n2", "traceback": "Traceback..."},
    )
    assert not server._run_checkpoint_path(run["id"]).exists()
    r = client.post(f"/api/workflow_runs/{run['id']}/retry_from_checkpoint")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# 3. cleanup statuses filter
# --------------------------------------------------------------------------- #
def test_cleanup_statuses_only_deletes_requested_terminal(client, monkeypatch):
    ok_model = ScriptedChatModel(scripts=[script(text="fine")])
    boom = _SeqModel(fail_on=1, fail_msg="boom-cleanup")
    _patch_build_model(monkeypatch, [ok_model, boom])
    wf = store.create_def(_llm_chain_wf())

    done_run = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]
    client.post(f"/api/workflow_runs/{done_run['id']}/_await")
    assert store.get_run(done_run["id"])["status"] == "done"
    failed_run = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]
    client.post(f"/api/workflow_runs/{failed_run['id']}/_await")
    assert store.get_run(failed_run["id"])["status"] == "failed"
    paused_run = store.create_run(wf)
    server._set_run_status(paused_run["id"], "paused")

    r = client.post("/api/workflow_runs/cleanup", json={"statuses": ["done"]})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert store.get_run(done_run["id"]) is None          # done deleted
    assert store.get_run(failed_run["id"]) is not None    # failed untouched
    assert store.get_run(paused_run["id"]) is not None    # paused untouched

    # non-terminal statuses in the filter are ignored (intersected away)
    r2 = client.post("/api/workflow_runs/cleanup", json={"statuses": ["paused"]})
    assert r2.json()["deleted"] == 0
    assert store.get_run(paused_run["id"]) is not None

    r3 = client.post("/api/workflow_runs/cleanup", json={"statuses": ["failed"]})
    assert r3.json()["deleted"] == 1
    assert store.get_run(failed_run["id"]) is None


# --------------------------------------------------------------------------- #
# 4. summarize-from-session: last_n + bounded retry loop
# --------------------------------------------------------------------------- #
class _RecordingModel:
    """Returns scripted replies and records every ainvoke message list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    async def ainvoke(self, messages, *a, **k):
        self.calls.append(list(messages))
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="")


def _seed_session(slug: str, sid: str) -> None:
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


_VALID_DSL = json.dumps(
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


def test_summarize_last_n_limits_trace(client, monkeypatch):
    sid = "sess-ux-lastn"
    _seed_session("default", sid)
    model = _RecordingModel([AIMessage(content=_VALID_DSL)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: model)

    r = client.post(
        "/api/workflows/summarize-from-session", json={"session_id": sid, "last_n": 1}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    # last_n=1 keeps only the final message ("here is the summary")
    trace = model.calls[0][1].content
    assert "here is the summary" in trace
    assert "list the open PRs" not in trace

    # control: without last_n the whole history is traced
    model2 = _RecordingModel([AIMessage(content=_VALID_DSL)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: model2)
    r2 = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    assert r2.json()["ok"] is True
    trace2 = model2.calls[0][1].content
    assert "list the open PRs" in trace2 and "here is the summary" in trace2


def test_summarize_retry_loop_bounded_at_3_attempts(client, monkeypatch):
    sid = "sess-ux-retry"
    _seed_session("default", sid)
    model = _RecordingModel([AIMessage(content="sorry, no JSON today")] * 5)
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: model)

    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "raw" in body
    # the loop must terminate: exactly 3 model attempts, never more
    assert len(model.calls) == 3, f"retry loop ran {len(model.calls)} attempts"
    # attempts 2+ carry the corrective hint
    assert "Previous attempt error" in model.calls[1][1].content


def test_summarize_retry_recovers_on_second_attempt(client, monkeypatch):
    sid = "sess-ux-recover"
    _seed_session("default", sid)
    near_miss = json.dumps(
        {
            "name": "Bad Entry",
            "entry": "zz",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "g"}],
            "edges": [],
        }
    )
    model = _RecordingModel([AIMessage(content=near_miss), AIMessage(content=_VALID_DSL)])
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: model)

    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    body = r.json()
    assert body["ok"] is True, body
    assert len(model.calls) == 2
    # the validation errors were fed back to the model
    assert "Previous attempt DSL errors" in model.calls[1][1].content
