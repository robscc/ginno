"""Goal store — per-project goals.json keyed by session_id (design: goal-design.md).

One goal per session (Codex parity): objective + status machine + cumulative
usage (time/turns). No token budget (removed in review). Persisted per project
at ``~/.ginno/projects/<slug>/goals.json``:

    { "<session_id>": { goal_id, objective, status, time_used_seconds,
                        turns_used, agent_id, created_at, updated_at } }

``goal_id`` is an optimistic-concurrency token: it changes on every goal
replacement, and mutating helpers accept ``expected_goal_id`` so a stale async
update (e.g. a lagging continuation driver) can never clobber a goal the user
just replaced.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from .. import paths

# Status machine (design §0): model may only set complete/blocked (via the
# goal_update tool); paused is user-controlled; usage_limited is system-set.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_BLOCKED = "blocked"
STATUS_USAGE_LIMITED = "usage_limited"
STATUS_COMPLETE = "complete"

ALL_STATUSES = {STATUS_ACTIVE, STATUS_PAUSED, STATUS_BLOCKED, STATUS_USAGE_LIMITED, STATUS_COMPLETE}
# Statuses that stop autonomous continuation but are resumable by the user.
STOPPED_STATUSES = {STATUS_PAUSED, STATUS_BLOCKED, STATUS_USAGE_LIMITED}

MAX_OBJECTIVE_CHARS = 4000


class GoalConflictError(ValueError):
    """Raised when creating a goal while an unfinished one exists."""


def validate_objective(objective: Any) -> str:
    text = (objective or "").strip() if isinstance(objective, str) else ""
    if not text:
        raise ValueError("objective required")
    if len(text) > MAX_OBJECTIVE_CHARS:
        raise ValueError(f"objective exceeds {MAX_OBJECTIVE_CHARS} chars")
    return text


def is_open(goal: dict[str, Any] | None) -> bool:
    """True when a goal exists and is not complete (blocks fresh creation)."""
    return bool(goal) and goal.get("status") != STATUS_COMPLETE


def _read(slug: str) -> dict[str, dict[str, Any]]:
    p = paths.project_goals_path(slug)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _write(slug: str, goals: dict[str, dict[str, Any]]) -> None:
    p = paths.project_goals_path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(goals, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _normalize(g: dict[str, Any]) -> dict[str, Any]:
    g.setdefault("time_used_seconds", 0)
    g.setdefault("turns_used", 0)
    g.setdefault("agent_id", None)
    return g


def get_goal(slug: str, session_id: str) -> dict[str, Any] | None:
    g = _read(slug).get(session_id)
    return _normalize(g) if g else None


def list_goals(slug: str) -> dict[str, dict[str, Any]]:
    return {sid: _normalize(g) for sid, g in _read(slug).items()}


def _new_goal(slug: str, session_id: str, objective: str, agent_id: str | None) -> dict[str, Any]:
    now = time.time()
    return {
        "goal_id": uuid.uuid4().hex,
        "objective": objective,
        "status": STATUS_ACTIVE,
        "time_used_seconds": 0,
        "turns_used": 0,
        "agent_id": agent_id,
        "created_at": now,
        "updated_at": now,
    }


def create_goal(
    slug: str, session_id: str, objective: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Create a goal for the session. Fails when an unfinished goal exists
    (Codex parity: complete the existing goal first)."""
    text = validate_objective(objective)
    goals = _read(slug)
    if is_open(goals.get(session_id)):
        raise GoalConflictError("session has an unfinished goal; finish or clear it first")
    goal = _new_goal(slug, session_id, text, agent_id)
    goals[session_id] = goal
    _write(slug, goals)
    return dict(goal)


def replace_goal(
    slug: str, session_id: str, objective: str, agent_id: str | None = None
) -> dict[str, Any]:
    """Unconditionally replace the session's goal (fresh goal_id, usage reset).
    Callers own the "confirm before replacing an unfinished goal" UX."""
    text = validate_objective(objective)
    goals = _read(slug)
    goal = _new_goal(slug, session_id, text, agent_id)
    goals[session_id] = goal
    _write(slug, goals)
    return dict(goal)


def _mutate(
    slug: str,
    session_id: str,
    fn,
    expected_goal_id: str | None = None,
) -> dict[str, Any] | None:
    """Read-modify-write one goal. Returns None when the session has no goal
    or the optimistic-concurrency check fails (stale caller)."""
    goals = _read(slug)
    g = goals.get(session_id)
    if not g:
        return None
    if expected_goal_id is not None and g.get("goal_id") != expected_goal_id:
        return None
    fn(_normalize(g))
    g["updated_at"] = time.time()
    goals[session_id] = g
    _write(slug, goals)
    return dict(g)


def update_status(
    slug: str, session_id: str, status: str, expected_goal_id: str | None = None
) -> dict[str, Any] | None:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown goal status: {status}")

    def apply(g: dict[str, Any]) -> None:
        g["status"] = status

    return _mutate(slug, session_id, apply, expected_goal_id)


def update_objective(
    slug: str, session_id: str, objective: str, expected_goal_id: str | None = None
) -> dict[str, Any] | None:
    text = validate_objective(objective)

    def apply(g: dict[str, Any]) -> None:
        g["objective"] = text

    return _mutate(slug, session_id, apply, expected_goal_id)


def account_turn(
    slug: str, session_id: str, elapsed_seconds: float, expected_goal_id: str | None = None
) -> dict[str, Any] | None:
    """Lightweight per-turn accounting (design §4.3.4): accumulate wall time
    and turn count while the goal is active. No token accounting (budget was
    removed in review)."""
    dt = max(0.0, float(elapsed_seconds or 0.0))

    def apply(g: dict[str, Any]) -> None:
        # Count every goal turn — including the one that TERMINATES the goal:
        # the model marks complete/blocked mid-turn, before this accounting
        # runs, and that turn still belonged to the goal. Only paused goals
        # don't accrue (the driver doesn't run turns for them).
        if g.get("status") == STATUS_PAUSED:
            return
        g["time_used_seconds"] = int(g.get("time_used_seconds", 0) + round(dt))
        g["turns_used"] = int(g.get("turns_used", 0)) + 1

    return _mutate(slug, session_id, apply, expected_goal_id)


def clear_goal(slug: str, session_id: str) -> bool:
    """Delete the session's goal entirely (usage record included)."""
    goals = _read(slug)
    if session_id not in goals:
        return False
    del goals[session_id]
    _write(slug, goals)
    return True
