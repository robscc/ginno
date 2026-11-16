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


# ---- archive_goal (lifecycle finish, 2026-11-16) -------------------------


def test_archive_complete_moves_to_history(isolated_home):
    store.create_goal(SLUG, SID, "Ship the report")
    store.update_status(SLUG, SID, "complete")
    archived = store.archive_goal(SLUG, SID)
    assert archived is not None
    assert archived["objective"] == "Ship the report"
    assert archived["status"] == "complete"
    assert archived["archived_at"] > 0
    # active store no longer holds it
    assert store.get_goal(SLUG, SID) is None
    # history is append-only jsonl with the full record
    from ginno_runtime import paths

    hp = paths.project_goals_path(SLUG).with_name("goals-history.jsonl")
    lines = hp.read_text().strip().splitlines()
    assert len(lines) == 1
    import json

    rec = json.loads(lines[0])
    assert rec["objective"] == "Ship the report"
    assert rec["archived_at"] == archived["archived_at"]


def test_archive_non_complete_is_noop(isolated_home):
    store.create_goal(SLUG, SID, "still working")
    assert store.archive_goal(SLUG, SID) is None
    assert store.get_goal(SLUG, SID)["status"] == "active"


def test_archive_missing_returns_none(isolated_home):
    assert store.archive_goal(SLUG, "nope") is None


def test_archive_history_appends(isolated_home):
    store.create_goal(SLUG, "s1", "first")
    store.update_status(SLUG, "s1", "complete")
    store.create_goal(SLUG, "s2", "second")
    store.update_status(SLUG, "s2", "complete")
    store.archive_goal(SLUG, "s1")
    store.archive_goal(SLUG, "s2")
    from ginno_runtime import paths

    hp = paths.project_goals_path(SLUG).with_name("goals-history.jsonl")
    assert len(hp.read_text().strip().splitlines()) == 2


def test_archive_preserves_usage(isolated_home):
    store.create_goal(SLUG, SID, "accounted")
    store.account_turn(SLUG, SID, 40.0)  # account_turn rounds to int seconds
    store.update_status(SLUG, SID, "complete")
    archived = store.archive_goal(SLUG, SID)
    assert archived["time_used_seconds"] == 40
    assert archived["turns_used"] == 1
