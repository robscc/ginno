"""Unit tests for the goal tools (goal-design.md §4.2)."""

from __future__ import annotations

import pytest

from ginno_runtime.goals import store as goal_store
from ginno_runtime.tools.goal_tools import build_goal_tools

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "sess-goal"


def _tools(isolated_home):
    return {t.name: t for t in build_goal_tools(SLUG, SID)}


def test_tool_names(isolated_home):
    tools = _tools(isolated_home)
    assert set(tools) == {"goal_get", "goal_create", "goal_update"}


def test_get_no_goal(isolated_home):
    tools = _tools(isolated_home)
    assert tools["goal_get"].invoke({}) == "(no goal)"


def test_create_and_get(isolated_home):
    tools = _tools(isolated_home)
    out = tools["goal_create"].invoke({"objective": "Write the report"})
    assert "created goal" in out
    got = tools["goal_get"].invoke({})
    assert "Write the report" in got
    assert "status: active" in got


def test_create_conflict(isolated_home):
    tools = _tools(isolated_home)
    tools["goal_create"].invoke({"objective": "first"})
    out = tools["goal_create"].invoke({"objective": "second"})
    assert "cannot create goal" in out


def test_update_complete_and_blocked_only(isolated_home):
    tools = _tools(isolated_home)
    tools["goal_create"].invoke({"objective": "x"})
    assert "only accepts" in tools["goal_update"].invoke({"status": "paused"})
    assert "only accepts" in tools["goal_update"].invoke({"status": "active"})
    assert "set to 'blocked'" in tools["goal_update"].invoke({"status": "blocked"})
    assert goal_store.get_goal(SLUG, SID)["status"] == "blocked"


def test_update_no_goal(isolated_home):
    tools = _tools(isolated_home)
    assert tools["goal_update"].invoke({"status": "complete"}) == "no goal to update"


def test_update_complete(isolated_home):
    tools = _tools(isolated_home)
    tools["goal_create"].invoke({"objective": "x"})
    out = tools["goal_update"].invoke({"status": "complete"})
    assert "set to 'complete'" in out
    assert goal_store.get_goal(SLUG, SID)["status"] == "complete"


def test_tools_have_descriptions(isolated_home):
    tools = _tools(isolated_home)
    for name, t in tools.items():
        assert t.description and len(t.description) > 20, name
