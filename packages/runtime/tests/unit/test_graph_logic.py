"""Unit tests for pure graph helpers + one real-graph drive with the fake LLM."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END

from ginno_runtime import paths
from ginno_runtime.agents.registry import AgentConfig
from ginno_runtime.graph import (
    _allowed_tool_names,
    build_agent_system_prompt,
    route_after_agent,
    tool_allowed,
)
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.tools.builtin import build_builtin_tools
from ginno_runtime.tools.render_tools import attach_ref, render_widget

pytestmark = pytest.mark.unit


def _agent(allow):
    return AgentConfig(id="t", name="T", system_prompt="You are T.", tools_allow=allow)


def test_tool_allowed_no_agent_allows_all():
    assert tool_allowed(None, "anything") is True


def test_tool_allowed_wildcard():
    assert tool_allowed(_agent(["*"]), "bash") is True


def test_tool_allowed_fnmatch():
    a = _agent(["read_*"])
    assert tool_allowed(a, "read_file") is True
    assert tool_allowed(a, "write_file") is False


def test_structured_tools_always_allowed():
    a = _agent(["read_file"])  # narrow allowlist
    assert tool_allowed(a, "render_widget") is True
    assert tool_allowed(a, "attach_ref") is True


def test_ask_user_available_to_every_role():
    """An agent that cannot ask is an agent that guesses — and this tool grants
    no capability, so a narrow allowlist must not lock it out. Note the
    deliberate contrast with install_skills, which stays gated."""
    a = _agent(["read_file"])
    assert tool_allowed(a, "ask_user") is True
    assert tool_allowed(a, "install_skills") is False
    assert tool_allowed(a, "workflow_run") is True
    assert tool_allowed(a, "workflow_get") is True
    assert tool_allowed(a, "artifact_register") is True
    assert tool_allowed(a, "use_skill") is True
    # management stays gated — a read-only analyst must not rewrite skills
    assert tool_allowed(a, "install_skills") is False
    assert tool_allowed(a, "uninstall_skill") is False


def test_skill_extra_allow_widens_after_activation(isolated_home):
    """Analyst + aliyun-bill: use_skill is always visible; bash only after
    the skill is on active_skills (script skills default to bash)."""
    from ginno_runtime.graph import _skill_extra_allow

    d = isolated_home / "skills" / "aliyun-bill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: aliyun-bill\ndescription: bills\ntrigger: both\n---\n\n"
        "跑 python3 aliyun_bill.py $ARGUMENTS\n",
        encoding="utf-8",
    )
    a = _agent(["read_file", "glob_files"])
    assert tool_allowed(a, "use_skill") is True
    assert tool_allowed(a, "bash") is False
    extra = _skill_extra_allow({"active_skills": ["aliyun-bill"], "project_slug": "default"})
    assert "bash" in extra
    assert tool_allowed(a, "bash", extra) is True
    t = isolated_home / "skills" / "todo-like"
    t.mkdir(parents=True)
    (t / "SKILL.md").write_text(
        "---\nname: todo-like\ndescription: t\ntrigger: both\n"
        "tools: [todo_list]\n---\n\nlist\n",
        encoding="utf-8",
    )
    extra2 = _skill_extra_allow({"active_skills": ["todo-like"], "project_slug": "default"})
    assert extra2 == ["todo_list"]
    assert "bash" not in extra2


async def test_use_skill_activates_skill_on_state(isolated_home):
    """After use_skill succeeds, active_skills carries the name so the next
    agent step can bind the skill's declared (or default bash) tools."""
    from langchain_core.messages import ToolMessage

    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import script_tool_call
    from ginno_runtime.tools.skill_tools import build_skill_tools

    d = isolated_home / "skills" / "aliyun-bill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: aliyun-bill\ndescription: bills\ntrigger: both\n---\n\n"
        "跑 python3 aliyun_bill.py $ARGUMENTS\n",
        encoding="utf-8",
    )
    model = ScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("use_skill", {"name": "aliyun-bill", "request": "overview"})]),
            script(text="ok, following the skill"),
        ]
    )
    graph = build_graph(
        model=model,
        project_slug="default",
        workspace="/tmp",
        mcp_tools=[],
        all_tools=build_skill_tools("default"),
    )
    cfg = {"configurable": {"thread_id": "t-skill", "project_slug": "default", "agent_id": "dev"}}
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="拉一下百炼账单")],
            "workspace": "/tmp",
            "project_slug": "default",
            "agent_id": "dev",
            "active_skills": [],
            "pending_tool_calls": [],
        },
        config=cfg,
    )
    assert "aliyun-bill" in (result.get("active_skills") or [])
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs and '<skill name="aliyun-bill">' in tool_msgs[0].content
    assert "overview" in tool_msgs[0].content


def test_allowed_tool_names_filters():
    tools = build_builtin_tools() + [render_widget, attach_ref]
    names = _allowed_tool_names(_agent(["read_file", "render_widget"]), tools)
    assert "read_file" in names
    assert "render_widget" in names
    assert "write_file" not in names


def test_route_after_agent():
    assert route_after_agent({"pending_tool_calls": [{"name": "x"}]}) == "permission"
    assert route_after_agent({"pending_tool_calls": []}) == END


def test_system_prompt_contains_persona_and_tools(isolated_home):
    tools = build_builtin_tools() + [render_widget, attach_ref]
    prompt = build_agent_system_prompt(_agent(["*"]), "p", tools)
    assert "You are T." in prompt
    assert "operating in this turn as **T**" in prompt
    assert "Tools available to you in this role" in prompt


def test_system_prompt_lists_no_tools_when_narrow(isolated_home):
    prompt = build_agent_system_prompt(_agent([]), "p", [])
    assert "(none" in prompt


async def test_graph_drives_no_tool_reply(isolated_home):
    paths.ensure_layout()
    from ginno_runtime.graph import build_graph

    model = ScriptedChatModel(scripts=[script(text="hi back")])
    graph = build_graph(model=model, project_slug="p", workspace="/tmp", mcp_tools=[])
    cfg = {"configurable": {"thread_id": "t", "project_slug": "p", "agent_id": "dev"}}
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="hi")],
            "workspace": "/tmp",
            "project_slug": "p",
            "agent_id": "dev",
            "active_skills": [],
            "pending_tool_calls": [],
        },
        config=cfg,
    )
    assert result["messages"][-1].content == "hi back"


def test_system_prompt_carries_workspace_and_skill_dirs(isolated_home):
    """F1/A1+A6 unfreeze: the stable layer must tell the model where it is
    and where skills live (2026-08 skill-install incident)."""
    from ginno_runtime.graph import build_stable_system

    ws_dir = str(paths.home() / "projects" / "p" / "sessions" / "s1")
    prompt = build_stable_system(
        _agent(["*"]), "p", build_builtin_tools(ws_dir), workspace=ws_dir
    )
    assert f"<workspace>{ws_dir}" in prompt
    assert str(paths.global_skills_dir()) in prompt
    assert "install_skills" in prompt  # guidance present for a ["*"] agent


def test_stable_system_instructs_artifact_registration_when_attach_ref_allowed(isolated_home):
    """Files written via bash/python heredocs are opaque to the Artifacts
    panel; the stable layer must tell the model to echo them through
    attach_ref (2026-08-17 turn 4b5a390f produced files that never showed
    up). Gated on attach_ref so agents without it don't get dead guidance."""
    from ginno_runtime.graph import build_stable_system
    from ginno_runtime.tools.render_tools import attach_ref, render_widget

    ws_dir = str(paths.home() / "projects" / "p" / "sessions" / "s2")
    # mirror the production assembly (graph.py: builtin + [render_widget, attach_ref])
    tools = build_builtin_tools(ws_dir) + [render_widget, attach_ref]
    assert any(t.name == "attach_ref" for t in tools)  # sanity: it's builtin

    prompt = build_stable_system(_agent(["*"]), "p", tools, workspace=ws_dir)
    assert "Artifact registration" in prompt
    assert "attach_ref(kind='file'" in prompt
    assert "write_file outputs" in prompt  # echo rule covers write_file too

    # gating: without attach_ref in the allowed set, no dead guidance
    no_attach = [t for t in tools if t.name != "attach_ref"]
    prompt2 = build_stable_system(_agent(["*"]), "p", no_attach, workspace=ws_dir)
    assert "Artifact registration" not in prompt2


async def test_raising_tool_is_contained_not_fatal(isolated_home):
    """The 2026-08 incident shape: a tool raises OSError mid-call. The turn
    must survive — the exception becomes an error ToolMessage the agent reads,
    not a 500 that kills the conversation."""
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import script_tool_call

    @tool
    def boom(x: str) -> str:
        """Always raises — like the old glob_files on a bad path."""
        raise OSError(22, "Invalid argument", "/.resolve/skills")

    model = ScriptedChatModel(
        scripts=[
            script(tool_calls=[script_tool_call("boom", {"x": "1"})]),
            script(text="recovered"),
        ]
    )
    graph = build_graph(
        model=model, project_slug="p", workspace="/tmp", mcp_tools=[], all_tools=[boom]
    )
    cfg = {"configurable": {"thread_id": "t2", "project_slug": "p", "agent_id": "dev"}}
    result = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="do it")],
            "workspace": "/tmp",
            "project_slug": "p",
            "agent_id": "dev",
            "active_skills": [],
            "pending_tool_calls": [],
        },
        config=cfg,
    )
    msgs = result["messages"]
    assert msgs[-1].content == "recovered"  # the loop continued past the error
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert tool_msgs and tool_msgs[0].status == "error"
    assert "Invalid argument" in tool_msgs[0].content
