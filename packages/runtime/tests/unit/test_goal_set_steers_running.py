"""Unit: setting a goal while a turn runs steers the running turn (2026-11-16).

Goal continuation only fires when idle; without this steer the running model
never learns about the new objective ("set a goal while running, nothing
happened"). The PUT handler must enqueue a steer iff the session is running.
"""

from __future__ import annotations

import pytest

from ginno_runtime import server_shared
from ginno_runtime.api import sessions
from ginno_runtime.goals import store

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SLUG = "default"
SID = "sess-steer"


@pytest.fixture(autouse=True)
def _clean_stash():
    """test_steering asserts the GLOBAL stash is empty — never leak our
    entries into later tests (root conftest clears _RUNNING_TURNS, not stash)."""
    yield
    server_shared._STEER_STASH.pop(SID, None)


def _wire(monkeypatch, running: bool):
    monkeypatch.setattr(
        sessions,
        "_SESSIONS",
        {SID: {"session_id": SID, "project_slug": SLUG, "agent_id": "dev"}},
    )

    async def _noop_event(*a, **k):
        pass

    monkeypatch.setattr(sessions, "_emit_goal_event", _noop_event)
    monkeypatch.setattr(sessions, "_start_goal_driver", lambda sid: None)
    server_shared._STEER_STASH.pop(SID, None)
    if running:
        sessions._RUNNING_TURNS[SID] = "turn-live"


async def test_set_goal_while_running_steers(isolated_home, monkeypatch):
    _wire(monkeypatch, running=True)
    r = await sessions.set_session_goal(SID, {"objective": "a2a over ssh remote dispatch"})
    assert r["ok"], r
    stash = server_shared._STEER_STASH.get(SID) or []
    assert any(e["steer_id"].startswith("goal-set-") for e in stash), stash
    assert "a2a over ssh remote dispatch" in stash[-1]["text"]
    # goal itself persisted active
    assert store.get_goal(SLUG, SID)["status"] == "active"


async def test_set_goal_idle_does_not_steer(isolated_home, monkeypatch):
    _wire(monkeypatch, running=False)
    r = await sessions.set_session_goal(SID, {"objective": "idle goal"})
    assert r["ok"], r
    assert not (server_shared._STEER_STASH.get(SID) or [])


async def test_steer_objective_truncated(isolated_home, monkeypatch):
    _wire(monkeypatch, running=True)
    long_obj = "x" * 2000
    r = await sessions.set_session_goal(SID, {"objective": long_obj})
    assert r["ok"], r
    stash = server_shared._STEER_STASH.get(SID) or []
    # steer text carries a bounded excerpt, not the full 4000-char objective
    assert len(stash[-1]["text"]) < 1500
    assert "…" in stash[-1]["text"]