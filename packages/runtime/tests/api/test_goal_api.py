"""API tests for the session goal endpoints (goal-design.md §4.4)."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def test_goal_lifecycle(create_session, client):
    sid = create_session([script(text="ok")])

    # fresh session → no goal
    r = client.get(f"/api/sessions/{sid}/goal")
    assert r.status_code == 200 and r.json()["goal"] is None

    # set objective → active goal
    r = client.put(f"/api/sessions/{sid}/goal", json={"objective": "Write the report"})
    assert r.json()["ok"] is True
    goal = r.json()["goal"]
    assert goal["status"] == "active"
    assert goal["objective"] == "Write the report"
    first_id = goal["goal_id"]

    # replacing an unfinished goal needs confirm
    r = client.put(f"/api/sessions/{sid}/goal", json={"objective": "New objective"})
    assert r.json()["ok"] is False and r.json()["needs_confirm"] is True

    r = client.put(
        f"/api/sessions/{sid}/goal", json={"objective": "New objective", "confirm": True}
    )
    assert r.json()["ok"] is True
    assert r.json()["goal"]["goal_id"] != first_id  # fresh goal_id
    assert r.json()["goal"]["objective"] == "New objective"

    # pause / resume
    r = client.put(f"/api/sessions/{sid}/goal", json={"status": "paused"})
    assert r.json()["goal"]["status"] == "paused"
    r = client.put(f"/api/sessions/{sid}/goal", json={"status": "active"})
    assert r.json()["goal"]["status"] == "active"

    # model-only statuses are rejected via the user API
    r = client.put(f"/api/sessions/{sid}/goal", json={"status": "complete"})
    assert r.json()["ok"] is False
    r = client.put(f"/api/sessions/{sid}/goal", json={"status": "blocked"})
    assert r.json()["ok"] is False

    # clear
    r = client.delete(f"/api/sessions/{sid}/goal")
    assert r.json()["cleared"] is True
    assert client.get(f"/api/sessions/{sid}/goal").json()["goal"] is None


def test_goal_validation(create_session, client):
    sid = create_session([script(text="ok")])
    r = client.put(f"/api/sessions/{sid}/goal", json={"objective": "   "})
    assert r.json()["ok"] is False
    r = client.put(f"/api/sessions/{sid}/goal", json={"objective": "x" * 4001})
    assert r.json()["ok"] is False
    # no body → nothing to do
    r = client.put(f"/api/sessions/{sid}/goal", json={})
    assert r.json()["ok"] is False


def test_goal_unknown_session(client):
    assert client.get("/api/sessions/nope/goal").json()["ok"] is False
    assert client.put("/api/sessions/nope/goal", json={"objective": "x"}).json()["ok"] is False
    assert client.delete("/api/sessions/nope/goal").json()["ok"] is False


def test_goal_cascade_on_session_delete(create_session, client):
    from ginno_runtime.goals import store as goal_store

    sid = create_session([script(text="ok")])
    client.put(f"/api/sessions/{sid}/goal", json={"objective": "to be deleted"})
    assert goal_store.list_goals("default")

    client.delete(f"/api/sessions/{sid}")
    assert sid not in goal_store.list_goals("default")
