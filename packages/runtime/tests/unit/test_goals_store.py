"""Unit tests for the goal store (goal-design.md §4.1)."""

from __future__ import annotations

import pytest

from ginno_runtime.goals import store

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "sess-1"


def test_create_and_get(isolated_home):
    g = store.create_goal(SLUG, SID, "Ship the report", agent_id="research")
    assert g["objective"] == "Ship the report"
    assert g["status"] == "active"
    assert g["agent_id"] == "research"
    assert g["turns_used"] == 0
    got = store.get_goal(SLUG, SID)
    assert got["goal_id"] == g["goal_id"]


def test_get_missing_returns_none(isolated_home):
    assert store.get_goal(SLUG, "nope") is None


def test_create_conflict_when_open(isolated_home):
    store.create_goal(SLUG, SID, "first")
    with pytest.raises(store.GoalConflictError):
        store.create_goal(SLUG, SID, "second")


def test_create_allowed_after_complete(isolated_home):
    store.create_goal(SLUG, SID, "first")
    store.update_status(SLUG, SID, "complete")
    g = store.create_goal(SLUG, SID, "second")
    assert g["objective"] == "second"
    assert g["turns_used"] == 0  # fresh usage


def test_replace_resets_usage_and_goal_id(isolated_home):
    g1 = store.create_goal(SLUG, SID, "first")
    store.account_turn(SLUG, SID, 10.0)
    g2 = store.replace_goal(SLUG, SID, "replaced")
    assert g2["goal_id"] != g1["goal_id"]
    assert g2["objective"] == "replaced"
    assert g2["time_used_seconds"] == 0


def test_update_status(isolated_home):
    store.create_goal(SLUG, SID, "x")
    g = store.update_status(SLUG, SID, "paused")
    assert g["status"] == "paused"
    with pytest.raises(ValueError):
        store.update_status(SLUG, SID, "bogus")


def test_update_objective(isolated_home):
    store.create_goal(SLUG, SID, "old")
    g = store.update_objective(SLUG, SID, "new objective")
    assert g["objective"] == "new objective"


def test_account_turn_accumulates_only_when_active(isolated_home):
    store.create_goal(SLUG, SID, "x")
    store.account_turn(SLUG, SID, 12.4)
    store.account_turn(SLUG, SID, 7.6)
    g = store.get_goal(SLUG, SID)
    assert g["turns_used"] == 2
    assert g["time_used_seconds"] == 20
    # paused turns are not accounted
    store.update_status(SLUG, SID, "paused")
    store.account_turn(SLUG, SID, 99.0)
    g = store.get_goal(SLUG, SID)
    assert g["turns_used"] == 2
    assert g["time_used_seconds"] == 20


def test_account_turn_counts_terminating_turn(isolated_home):
    # The model marks complete/blocked MID-turn, before accounting runs; that
    # turn still belonged to the goal and must be counted. Only paused is
    # excluded.
    store.create_goal(SLUG, SID, "x")
    store.update_status(SLUG, SID, "complete")
    store.account_turn(SLUG, SID, 5.0)
    g = store.get_goal(SLUG, SID)
    assert g["turns_used"] == 1 and g["time_used_seconds"] == 5
    # blocked likewise accrues
    store.update_status(SLUG, SID, "blocked")
    store.account_turn(SLUG, SID, 3.0)
    g = store.get_goal(SLUG, SID)
    assert g["turns_used"] == 2


def test_optimistic_concurrency(isolated_home):
    g = store.create_goal(SLUG, SID, "x")
    stale = g["goal_id"]
    store.replace_goal(SLUG, SID, "replaced")  # new goal_id
    # a stale mutation against the old goal_id must be a no-op
    assert store.update_status(SLUG, SID, "paused", expected_goal_id=stale) is None
    assert store.get_goal(SLUG, SID)["status"] == "active"
    # fresh goal_id works
    fresh = store.get_goal(SLUG, SID)["goal_id"]
    assert store.update_status(SLUG, SID, "paused", expected_goal_id=fresh)["status"] == "paused"


def test_clear(isolated_home):
    store.create_goal(SLUG, SID, "x")
    assert store.clear_goal(SLUG, SID) is True
    assert store.get_goal(SLUG, SID) is None
    assert store.clear_goal(SLUG, SID) is False


def test_validate_objective_limits(isolated_home):
    with pytest.raises(ValueError):
        store.create_goal(SLUG, SID, "   ")
    with pytest.raises(ValueError):
        store.create_goal(SLUG, SID, "x" * (store.MAX_OBJECTIVE_CHARS + 1))


def test_list_goals(isolated_home):
    store.create_goal(SLUG, "a", "one")
    store.create_goal(SLUG, "b", "two")
    goals = store.list_goals(SLUG)
    assert set(goals) == {"a", "b"}
    assert goals["a"]["objective"] == "one"
