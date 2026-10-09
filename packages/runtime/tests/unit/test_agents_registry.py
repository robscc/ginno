"""Unit tests for the agent registry: seeding, CRUD, tool allowlists, id immutability."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.agents import registry

pytestmark = pytest.mark.unit


def test_seed_creates_default_personas(isolated_home):
    registry.ensure_seeded()
    ids = {a.id for a in registry.list_agents()}
    assert {"dev", "research", "writer", "workflow-dev"} <= ids


def test_workflow_dev_prompt_names_human_node(isolated_home):
    registry.ensure_seeded()
    prompt = registry.get_agent("workflow-dev").system_prompt
    assert "human" in prompt
    assert "workflow_get" in prompt
    assert "workflow_get" in registry.get_agent("workflow-dev").tools_allow


def test_ensure_workflow_dev_lands_on_upgraded_install(isolated_home):
    registry.ensure_seeded()
    registry.delete_agent("workflow-dev")
    assert registry.get_agent("workflow-dev") is None
    registry.ensure_workflow_dev()
    landed = registry.get_agent("workflow-dev")
    assert landed is not None
    assert landed.id == "workflow-dev"
    assert "human" in landed.system_prompt
    # idempotent
    registry.ensure_workflow_dev()
    assert registry.get_agent("workflow-dev").id == "workflow-dev"


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


# ------------------------- agents.order sorting ------------------------- #
def _write_order(home, order):
    """Directly materialize agents.order in the isolated settings.json."""
    sp = home / "settings.json"
    data = json.loads(sp.read_text() or "{}") if sp.exists() else {}
    data["agents"] = {**(data.get("agents") or {}), "order": order}
    sp.write_text(json.dumps(data))


def test_order_reorders_and_appends_missing(isolated_home):
    registry.ensure_seeded()
    _write_order(isolated_home, ["writer", "dev"])
    ids = [a.id for a in registry.list_agents()]
    # in-order agents first, the rest appended alphabetically
    assert ids[:2] == ["writer", "dev"]
    assert ids[2:] == sorted(ids[2:])


def test_order_stale_ids_ignored(isolated_home):
    registry.ensure_seeded()
    # "ghost" no longer exists — silently dropped, no error
    _write_order(isolated_home, ["dev", "ghost", "research"])
    ids = [a.id for a in registry.list_agents()]
    assert "ghost" not in ids
    assert ids[:2] == ["dev", "research"]


def test_no_order_falls_back_alphabetical(isolated_home):
    # 键缺失 = 排序功能落地前的行为（字母序），老安装零迁移
    registry.ensure_seeded()
    ids = [a.id for a in registry.list_agents()]
    assert ids == sorted(ids)


def test_set_agent_order_roundtrip(isolated_home):
    registry.ensure_seeded()
    registry.set_agent_order(["research", "dev", "writer", "workflow-dev"])
    ids = [a.id for a in registry.list_agents()]
    assert ids == ["research", "dev", "writer", "workflow-dev"]
    # 读-改-写不得碰掉 settings 里已有的其它键
    data = json.loads((isolated_home / "settings.json").read_text())
    assert data["agents"]["order"][0] == "research"
