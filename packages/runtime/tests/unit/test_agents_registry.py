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


# ------------------- subagent_models (model-assignment P1) ------------------- #
def _seed_type(home, name="explore", fm_model=None):
    """落一个合法类型文件并失效注册表缓存（缓存按目录 mtime 续命）。"""
    from ginno_runtime import subagent_types as st

    d = home / "agents" / "subagents"
    d.mkdir(parents=True, exist_ok=True)
    fm = f"name: {name}\ndescription: test type\n"
    if fm_model:
        fm += f"model: {fm_model}\n"
    (d / f"{name}.md").write_text(f"---\n{fm}---\n\nbody\n", encoding="utf-8")
    st._CACHE = None
    st._DIR_STATE = None


def test_validate_subagent_models_roundtrip(isolated_home):
    _seed_type(isolated_home, "explore")
    out = registry.validate_subagent_models(
        {"explore": {"provider": "p1", "model": "m1"}, "ghost": None}
    )
    # null（UI 的 Inherit 项）规范化为缺席
    assert out == {"explore": {"provider": "p1", "model": "m1"}}


def test_validate_subagent_models_rejects_unknown_type(isolated_home):
    _seed_type(isolated_home, "explore")
    with pytest.raises(ValueError, match="unknown sub-agent type"):
        registry.validate_subagent_models({"nope": {"provider": "p1", "model": "m1"}})


def test_validate_subagent_models_enforces_binding(isolated_home, monkeypatch):
    _seed_type(isolated_home, "explore")
    monkeypatch.setattr(
        registry.prov_mod, "get_config", lambda pid: {"name": pid, "models": ["m1"]}
    )
    with pytest.raises(registry.AgentModelBindingError):
        registry.validate_subagent_models({"explore": {"provider": "p1", "model": "m2"}})


def test_update_agent_persists_and_merges_subagent_models(isolated_home):
    registry.ensure_seeded()
    _seed_type(isolated_home, "explore")
    _seed_type(isolated_home, "researcher")
    registry.update_agent(
        "dev", {"subagent_models": {"explore": {"provider": "p1", "model": "m1"}}}
    )
    assert registry.get_agent("dev").subagent_models == {
        "explore": {"provider": "p1", "model": "m1"}
    }
    # 不带该字段的更新走 merge，已存覆盖不被清空
    registry.update_agent("dev", {"name": "Dev 2"})
    assert registry.get_agent("dev").subagent_models["explore"]["model"] == "m1"
    assert registry.get_agent("dev").name == "Dev 2"


def test_read_sanitizes_malformed_subagent_models(isolated_home, caplog):
    """手改文件塞进畸形值：容忍清洗（丢条目+警告），不能废掉整个 agent。"""
    import logging

    registry.ensure_seeded()
    p = isolated_home / "agents" / "dev.json"
    data = json.loads(p.read_text())
    data["subagent_models"] = {"ghost-type": {"provider": "p1", "model": "m1"}, "bad": 42}
    p.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        cfg = registry.get_agent("dev")
    assert cfg is not None
    assert cfg.subagent_models == {}  # 未知类型 + 非 dict 条目都被清掉
