"""Unit tests for the WorldState engine (plan C1/C2, sections A1/A3/A4/A6/A7)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from ginno_runtime import paths
from ginno_runtime.world_state import (
    UPDATE_MSG_PREFIX,
    EnvironmentSection,
    McpSection,
    PermissionsSection,
    SessionCtx,
    SkillsSection,
    WorldState,
    context_settings,
    diff_snapshots,
    render_update,
    sync_world_state,
    world_state_path,
)

pytestmark = pytest.mark.unit


def ctx(**kw) -> SessionCtx:
    base = dict(session_id="s1", project_slug="default", agent_id=None)
    base.update(kw)
    return SessionCtx(**base)


# --------------------------------------------------------------------------- #
# environment section (A1)
# --------------------------------------------------------------------------- #
def test_environment_snapshot_fields():
    snap = EnvironmentSection().snapshot(ctx())
    assert snap["date"] == datetime.now().astimezone().strftime("%Y-%m-%d")
    assert snap["weekday"].startswith("星期")
    assert "UTC" in snap["tz"]
    assert snap["os"]
    assert snap["ginno_home"] == str(paths.home())
    assert snap["project_slug"] == "default"


def test_environment_render_shape():
    sec = EnvironmentSection()
    out = sec.render(sec.snapshot(ctx()))
    assert out.startswith("<environment>") and out.endswith("</environment>")
    assert "<date>" in out and "<os>" in out
    # no clock time in the stable layer (cache discipline)
    assert datetime.now().strftime("%H:%M") not in out


def test_environment_update_only_on_date_change():
    sec = EnvironmentSection()
    base = sec.snapshot(ctx())
    assert sec.update_text(base, dict(base)) is None
    changed = dict(base, date="2030-01-01", weekday="星期二")
    assert "2030-01-01" in sec.update_text(base, changed)


# --------------------------------------------------------------------------- #
# permissions section (A3)
# --------------------------------------------------------------------------- #
def test_permissions_reflects_bypass(isolated_home):
    paths.ensure_layout()
    sec = PermissionsSection()
    snap = sec.snapshot(ctx())
    # isolated_home default settings: bypass_permissions True (seeded layout)
    assert snap["bypass"] in (True, False)
    # toggle and re-read live
    sp = paths.settings_path()
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = False
    sp.write_text(json.dumps(s))
    snap2 = sec.snapshot(ctx())
    assert snap2["bypass"] is False
    assert "审批模式" in sec.render(snap2)
    text = sec.update_text(snap, snap2)
    assert text is not None and "审批模式" in text


# --------------------------------------------------------------------------- #
# agent section (A4)
# --------------------------------------------------------------------------- #
def test_agent_section_prompt_edit_detected(isolated_home):
    paths.ensure_layout()
    from ginno_runtime import agents as agents_reg

    agent = agents_reg.list_agents()[0]
    c = ctx(agent_id=agent.id, all_tool_names=["read_file", "render_widget"])
    from ginno_runtime.world_state import AgentSection

    sec = AgentSection()
    before = sec.snapshot(c)
    assert before["agent_id"] == agent.id
    assert before["name"] == agent.name
    assert before["tool_count"] >= 1

    # simulate a prompt edit through the registry store (Settings → Agent 管理)
    new_prompt = (agent.system_prompt or "") + "\n(new rule)"
    agents_reg.update_agent(agent.id, {"system_prompt": new_prompt})

    after = sec.snapshot(c)
    assert after["prompt_hash"] != before["prompt_hash"]
    text = sec.update_text(before, after)
    assert text is not None and "角色设定" in text


def test_agent_section_switch_detected(isolated_home):
    paths.ensure_layout()
    from ginno_runtime import agents as agents_reg
    from ginno_runtime.world_state import AgentSection

    agents = agents_reg.list_agents()
    assert len(agents) >= 2, "seed registry should have >=2 agents"
    a, b = agents[0], agents[1]
    sec = AgentSection()
    before = sec.snapshot(ctx(agent_id=a.id))
    after = sec.snapshot(ctx(agent_id=b.id))
    text = sec.update_text(before, after)
    assert text is not None and b.name in text and "切换" in text


# --------------------------------------------------------------------------- #
# skills section (A6 budget)
# --------------------------------------------------------------------------- #
def test_skills_budget_truncates(isolated_home):
    home = isolated_home
    for i in range(30):
        d = home / "skills" / f"skill{i:02d}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: skill{i:02d}\ndescription: does many useful things number {i}\n"
            "trigger: both\n---\n\nBody.\n",
            encoding="utf-8",
        )
    sp = paths.settings_path()
    settings = json.loads(sp.read_text()) if sp.exists() else {}
    settings["context"] = {"skills_index_max_chars": 300}
    paths.settings_path().write_text(json.dumps(settings))

    sec = SkillsSection()
    snap = sec.snapshot(ctx(project_slug="default"))
    assert snap is not None and len(snap["names"]) == 30
    assert len(snap["index"]) <= 300 + 100  # budget + tail note slack
    assert "未列出" in snap["index"]  # tail note about dropped skills


def test_skills_change_detection(isolated_home):
    sec = SkillsSection()
    assert sec.snapshot(ctx()) is None  # no skills
    d = isolated_home / "skills" / "hello"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: hello\ndescription: hi\ntrigger: both\n---\n\nB.\n", encoding="utf-8"
    )
    after = sec.snapshot(ctx())
    assert after["names"] == ["hello"]
    text = sec.update_text({}, after)
    assert text and "hello" in text


# --------------------------------------------------------------------------- #
# mcp section (A7)
# --------------------------------------------------------------------------- #
def test_mcp_section_change_text():
    sec = McpSection()
    assert sec.snapshot(ctx(mcp_tool_names=[])) is None
    before = sec.snapshot(ctx(mcp_tool_names=["a", "b"]))
    after = sec.snapshot(ctx(mcp_tool_names=["a", "b", "c"]))
    text = sec.update_text(before, after)
    assert text and "2 → 3" in text
    assert sec.update_text(before, before) is None


# --------------------------------------------------------------------------- #
# WorldState assembly + diff + update rendering
# --------------------------------------------------------------------------- #
def test_world_state_render_contains_sections(isolated_home):
    paths.ensure_layout()
    ws = WorldState(ctx(agent_id=None))
    out = ws.render_system()
    assert "<environment>" in out
    assert "<permissions>" in out
    assert "operating in this turn as" in out  # agent section


def test_diff_and_render_update_merged(isolated_home):
    paths.ensure_layout()
    old = {
        "environment": {"date": "2026-08-03", "weekday": "星期一"},
        "mcp": {"count": 1, "hash": "abc"},
    }
    new = {
        "environment": {"date": "2026-08-04", "weekday": "星期二"},
        "mcp": {"count": 2, "hash": "def"},
    }
    changes = diff_snapshots(old, new)
    assert {c[0] for c in changes} == {"environment", "mcp"}
    text, chip = render_update(changes)
    assert text.startswith(UPDATE_MSG_PREFIX)
    assert "2026-08-04" in text and "1 → 2" in text
    assert {c["section"] for c in chip} == {"environment", "mcp"}


def test_diff_removed_section():
    old = {"mcp": {"count": 2, "hash": "x"}}
    changes = diff_snapshots(old, {})
    assert changes and changes[0][0] == "mcp"


def test_sync_world_state_baseline_flow(isolated_home):
    paths.ensure_layout()
    c = ctx(session_id="sess-x")
    # first sync: baseline recorded, nothing announced
    text, chip = sync_world_state(c)
    assert text is None and chip == []
    assert world_state_path("default", "sess-x").exists()

    # unchanged world: still silent
    text, chip = sync_world_state(c)
    assert text is None and chip == []

    # simulate date rollover by editing the stored baseline
    p = world_state_path("default", "sess-x")
    data = json.loads(p.read_text())
    data["snapshot"]["environment"]["date"] = "1999-12-31"
    p.write_text(json.dumps(data))
    text, chip = sync_world_state(c)
    assert text is not None and "日期已更新" in text
    assert chip and chip[0]["section"] == "environment"


def test_context_settings_defaults_and_override(isolated_home):
    s = context_settings()
    assert s["world_state"] is True
    assert s["compact_threshold_tokens"] == 100000
    paths.settings_path().write_text(
        json.dumps({"context": {"compact_threshold_tokens": 123, "bogus_key": 1}})
    )
    s2 = context_settings()
    assert s2["compact_threshold_tokens"] == 123
    assert "bogus_key" not in s2
