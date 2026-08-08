"""E2E: goal autonomous continuation loop (goal-design.md §4.3.3).

Drives the REAL driver: set a goal via REST, the server-side driver injects a
continuation turn headlessly, the scripted model works the turn and calls
goal_update to terminate. Verifies completion, blocked, pause-stops-loop, and
the agent-switch auto-pause rule.
"""

from __future__ import annotations

import time

import pytest
from conftest import events_of

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from ginno_runtime import server
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


class RaisingModel(BaseChatModel):
    """A model whose every call fails, to exercise the driver's error mapping."""

    message: str = "boom"

    @property
    def _llm_type(self) -> str:
        return "raising"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError(self.message)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError(self.message)
        yield  # pragma: no cover


def test_goal_error_status_mapping():
    assert server._goal_error_status("Error code 429: rate limit exceeded") == "usage_limited"
    assert server._goal_error_status("insufficient_quota") == "usage_limited"
    assert server._goal_error_status("Provider overloaded") == "usage_limited"
    assert server._goal_error_status("ValueError: bad tool arg") == "blocked"
    assert server._goal_error_status("") == "blocked"


def _wait_goal(client, sid, predicate, timeout=15.0, interval=0.1):
    """Poll the goal endpoint until predicate(goal) is true (or timeout)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(f"/api/sessions/{sid}/goal").json().get("goal")
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"goal did not reach state; last={last}")


@pytest.fixture(autouse=True)
def fast_grace(monkeypatch):
    # Shrink the inter-turn grace so continuation runs immediately in tests.
    monkeypatch.setattr("ginno_runtime.api.sessions.GOAL_GRACE_S", 0.0)


def test_continuation_completes_goal(create_session, client):
    # Continuation turn 1: model marks the goal complete; turn 2: final text.
    model = [
        script(tool_calls=[script_tool_call("goal_update", {"status": "complete"})]),
        script(text="Goal achieved — report written."),
    ]
    sid = create_session(model)
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "Write the report"})

    goal = _wait_goal(client, sid, lambda g: g and g["status"] == "complete")
    assert goal["turns_used"] >= 1
    assert goal["time_used_seconds"] >= 0

    # The continuation turn landed in history as a short context row + reply.
    hist = client.get(f"/api/sessions/{sid}/history").json()
    ctx_rows = [
        b["text"]
        for m in hist.get("messages", [])
        if m.get("role") == "assistant"
        for b in m.get("blocks", [])
        if b.get("kind") == "context"
    ]
    # context rows ride system messages; also check assistant text survived
    texts = " ".join(
        b.get("text", "")
        for m in hist.get("messages", [])
        for b in m.get("blocks", [])
        if b.get("kind") == "text"
    )
    assert "Goal achieved" in texts


def test_erroring_turn_stops_driver_usage_limited(create_session, client):
    # A provider rate-limit failure must stop the loop as usage_limited, not
    # error-loop (P2-11).
    sid = create_session(RaisingModel(message="Error 429: rate limit exceeded"))
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "will fail"})
    goal = _wait_goal(
        client, sid, lambda g: g and g["status"] == "usage_limited", timeout=15
    )
    assert goal["status"] == "usage_limited"


def test_erroring_turn_stops_driver_blocked(create_session, client):
    # A non-rate-limit failure maps to blocked and stops the loop.
    sid = create_session(RaisingModel(message="ValueError: bad state"))
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "will fail"})
    goal = _wait_goal(client, sid, lambda g: g and g["status"] == "blocked", timeout=15)
    assert goal["status"] == "blocked"


def test_continuation_blocked_stops_loop(create_session, client):
    model = [
        script(tool_calls=[script_tool_call("goal_update", {"status": "blocked"})]),
        script(text="I am blocked: need API key from the user."),
    ]
    sid = create_session(model)
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "Sync Todoist"})

    goal = _wait_goal(client, sid, lambda g: g and g["status"] == "blocked")
    turns_at_blocked = goal["turns_used"]
    # The loop must stop: no further turns accrue once blocked.
    time.sleep(0.6)
    again = client.get(f"/api/sessions/{sid}/goal").json()["goal"]
    assert again["status"] == "blocked"
    assert again["turns_used"] == turns_at_blocked


def test_pause_stops_continuation(create_session, client, monkeypatch):
    # Give the pause a grace window to land before the first turn fires.
    monkeypatch.setattr("ginno_runtime.api.sessions.GOAL_GRACE_S", 2.0)
    # Model that would keep the loop alive if it ran; pause must prevent it.
    model = [script(text="still working…")] * 5
    sid = create_session(model)
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "Long task"})
    # Pause within the grace window → driver stops before running a turn.
    client.put(f"/api/sessions/{sid}/goal", json={"status": "paused"})

    goal = client.get(f"/api/sessions/{sid}/goal").json()["goal"]
    assert goal["status"] == "paused"
    time.sleep(2.6)  # past the grace; nothing should have run
    again = client.get(f"/api/sessions/{sid}/goal").json()["goal"]
    assert again["status"] == "paused"
    assert again["turns_used"] == 0


def test_agent_switch_auto_pauses_goal(create_session, client, monkeypatch):
    # Keep the driver from racing ahead while we flip the agent.
    monkeypatch.setattr("ginno_runtime.api.sessions.GOAL_GRACE_S", 2.0)
    model = [script(text="working")] * 5
    sid = create_session(model, agent_id="dev")  # goal snapshots agent_id=dev
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "Dev task"})
    # Switch the session to a different agent → goal must auto-pause.
    client.patch(f"/api/sessions/{sid}", json={"agent_id": "writer"})

    goal = _wait_goal(client, sid, lambda g: g and g["status"] == "paused", timeout=6)
    assert goal["status"] == "paused"


def test_goal_slash_command(ws_conv, create_session, client, monkeypatch):
    monkeypatch.setattr("ginno_runtime.api.sessions.GOAL_GRACE_S", 60.0)  # no auto-run during this test
    sid = create_session([script(text="ok")])

    with ws_conv(sid) as conv:
        # bare /goal with no goal set → usage notice
        conv.invoke("/goal")
        evs = conv.recv_until("message.end")
        notices = events_of(evs, "notice")
        assert notices and "Goal 用法" in notices[0]["message"]

        # /goal <objective> → sets the goal
        conv.invoke("/goal 调研并写出报告")
        evs = conv.recv_until("message.end")
        assert "目标已设定" in events_of(evs, "notice")[0]["message"]
        goal = client.get(f"/api/sessions/{sid}/goal").json()["goal"]
        assert goal["objective"] == "调研并写出报告" and goal["status"] == "active"

        # /goal pause → paused
        conv.invoke("/goal pause")
        conv.recv_until("message.end")
        assert client.get(f"/api/sessions/{sid}/goal").json()["goal"]["status"] == "paused"

        # /goal resume → active again
        conv.invoke("/goal resume")
        conv.recv_until("message.end")
        assert client.get(f"/api/sessions/{sid}/goal").json()["goal"]["status"] == "active"

        # /goal (bare) now shows the summary
        conv.invoke("/goal")
        evs = conv.recv_until("message.end")
        assert "当前目标" in events_of(evs, "notice")[0]["message"]

        # /goal clear → removed
        conv.invoke("/goal clear")
        conv.recv_until("message.end")
        assert client.get(f"/api/sessions/{sid}/goal").json()["goal"] is None


def test_goal_context_row_in_history(create_session, client):
    model = [
        script(tool_calls=[script_tool_call("goal_update", {"status": "complete"})]),
        script(text="done"),
    ]
    sid = create_session(model)
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "Render check"})
    _wait_goal(client, sid, lambda g: g and g["status"] == "complete")

    hist = client.get(f"/api/sessions/{sid}/history").json()
    ctx_texts = [
        b.get("text", "")
        for m in hist.get("messages", [])
        if m.get("role") == "system"
        for b in m.get("blocks", [])
        if b.get("kind") == "context"
    ]
    # The continuation message renders as a SHORT context row, not the full prompt
    rows = [t for t in ctx_texts if "目标推进" in t]
    assert rows, f"no goal context row in {ctx_texts}"
    assert "<ginno_goal" not in " ".join(rows)  # model scaffolding hidden
