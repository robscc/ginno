"""Unit tests for the agent registry: seeding, CRUD, tool allowlists, id immutability."""

from __future__ import annotations

import pytest

from ginno_runtime.agents import registry

pytestmark = pytest.mark.unit


def test_seed_creates_default_personas(isolated_home):
    registry.ensure_seeded()
    ids = {a.id for a in registry.list_agents()}
    assert {"dev", "research", "writer"} <= ids


def test_research_is_read_only_by_default(isolated_home):
    research = registry.get_agent("research")
    assert "*" not in research.tools_allow
    assert "read_file" in research.tools_allow


def test_dev_has_all_tools(isolated_home):
    assert registry.get_agent("dev").tools_allow == ["*"]


def test_create_and_get(isolated_home):
    registry.create_agent({"id": "qa", "name": "QA Agent", "tools_allow": ["read_file"]})
    assert registry.get_agent("qa").name == "QA Agent"


def test_create_requires_id(isolated_home):
    with pytest.raises(ValueError):
        registry.create_agent({"id": "", "name": "no id"})


def test_create_duplicate_raises(isolated_home):
    registry.ensure_seeded()
    with pytest.raises(ValueError):
        registry.create_agent({"id": "dev", "name": "dup"})


def test_update_id_is_immutable(isolated_home):
    registry.ensure_seeded()
    updated = registry.update_agent("dev", {"id": "hacked", "name": "Renamed"})
    assert updated.id == "dev"  # id unchanged
    assert updated.name == "Renamed"


def test_update_unknown_raises(isolated_home):
    with pytest.raises(ValueError):
        registry.update_agent("ghost", {"name": "x"})


def test_delete(isolated_home):
    registry.create_agent({"id": "tmp", "name": "Tmp"})
    assert registry.delete_agent("tmp") is True
    assert registry.delete_agent("tmp") is False


def test_ensure_todo_tools_migration(isolated_home):
    registry.ensure_seeded()
    registry.ensure_todo_tools()
    research = registry.get_agent("research")
    # research is non-"*" so it should gain a read-only todo pattern
    assert "todo_list" in research.tools_allow
    # dev already has "*" -> left untouched
    assert registry.get_agent("dev").tools_allow == ["*"]


def test_seed_research_has_discipline_prompt(isolated_home):
    registry.ensure_seeded()
    prompt = registry.get_agent("research").system_prompt
    assert "Research discipline:" in prompt
    assert "Verify before you claim" in prompt
    assert "Cite as you go" in prompt


def test_research_discipline_migration_upgrades_legacy_seed(isolated_home):
    registry.ensure_seeded()
    # Simulate an old install still carrying the one-liner seed prompt.
    registry.update_agent("research", {"system_prompt": registry._LEGACY_RESEARCH_PROMPT})
    registry.ensure_research_discipline()
    assert registry.get_agent("research").system_prompt == registry._RESEARCH_PROMPT


def test_research_discipline_migration_preserves_user_prompt(isolated_home):
    registry.ensure_seeded()
    custom = "You are my hand-tuned research persona. Behave differently."
    registry.update_agent("research", {"system_prompt": custom})
    registry.ensure_research_discipline()
    assert registry.get_agent("research").system_prompt == custom


def test_research_discipline_migration_is_idempotent(isolated_home):
    registry.ensure_seeded()
    registry.ensure_research_discipline()
    registry.ensure_research_discipline()
    assert registry.get_agent("research").system_prompt == registry._RESEARCH_PROMPT


def test_ensure_goal_tools_migration(isolated_home):
    registry.ensure_seeded()
    registry.ensure_goal_tools()
    # research / writer are non-"*" so they gain the goal pattern
    assert "goal_*" in registry.get_agent("research").tools_allow
    assert "goal_*" in registry.get_agent("writer").tools_allow
    # dev already has "*" -> left untouched
    assert registry.get_agent("dev").tools_allow == ["*"]
    # idempotent: running again does not duplicate the pattern
    registry.ensure_goal_tools()
    assert registry.get_agent("research").tools_allow.count("goal_*") == 1
