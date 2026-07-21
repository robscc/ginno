"""Main LangGraph: START → agent → permission → tools → agent (loop).

One compiled graph per session carries the UNION toolset. The per-turn
agent (state['agent_id']) selects: the persona system prompt, the tool
subset bound to the model, and the tools_allow enforcement in the
permission node. The system prompt is rebuilt every turn and is NOT
persisted into the checkpoint, so switching agents mid-session (manual
"Ask X" routing) takes effect on the very next turn while the message
history stays shared across agents in the same conversation.
"""

from __future__ import annotations

import fnmatch
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from . import agents as agents_reg
from .agents.memory import read_agent_memory
from .checkpointer import FileCheckpointer
from .permission.policy import PermissionPolicy
from .skills.loader import SkillLoader
from .state import AgentState
from .tools.builtin import build_builtin_tools
from .tools.render_tools import RENDER_TOOL_NAMES, attach_ref, render_widget
from .tools.todo_tools import ALL_TODO_TOOLS, TODO_TOOL_NAMES
from .tools.workflow_tools import ALL_WORKFLOW_TOOLS, WORKFLOW_TOOL_NAMES
from .tools.artifact_tools import ALL_ARTIFACT_TOOLS, ARTIFACT_TOOL_NAMES

# permission-node deny messages are tagged so the WS layer can resolve the
# matching "running" tool bubble (the model never streams these).
BLOCK_PREFIX = "[blocked:"


def _resolve_agent(agent_id: str | None):
    if agent_id:
        a = agents_reg.get_agent(agent_id)
        if a:
            return a
    lst = agents_reg.list_agents()
    return lst[0] if lst else None


def tool_allowed(agent, tool_name: str) -> bool:
    if tool_name in RENDER_TOOL_NAMES:
        return True  # structured-output tools are available to every agent
    if tool_name in WORKFLOW_TOOL_NAMES or tool_name in ARTIFACT_TOOL_NAMES:
        return True
    if not agent:
        return True
    allow = agent.tools_allow or ["*"]
    if "*" in allow:
        return True
    return any(fnmatch.fnmatch(tool_name, p) for p in allow)


def _allowed_tool_names(agent, all_tools) -> list[str]:
    return [t.name for t in all_tools if tool_allowed(agent, t.name)]


def build_agent_system_prompt(agent, project_slug: str, all_tools, query: str = "") -> str:
    name = agent.name if agent else "Agent"
    persona = (
        agent.system_prompt
        if agent and agent.system_prompt
        else "You are a helpful assistant."
    )
    parts = [persona, f"\nYou are operating in this turn as **{name}**."]
    allowed = _allowed_tool_names(agent, all_tools)
    parts.append(
        "Tools available to you in this role: "
        + (", ".join(allowed) or "(none — answer from knowledge only)")
        + ". Never call a tool outside this list."
    )
    parts.append(
        "Structured output: when a breakdown/status list is clearer than prose, call "
        "render_widget(kind='stat_list', data={'title': <str>, 'items': [{'label': <str>, "
        "'value': <str>, 'status': 'done'|'running'|'pending'|'ok'|'error'}]}) and follow it "
        "with a one-line summary. To attach a reference chip (file/workflow/doc) below your "
        "answer, call attach_ref(kind, name). Prefer these over long plain-text lists. "
        "These two tools render silently on the user's screen — do NOT quote or repeat "
        "their return values; just add a brief human summary."
    )
    if any(n.startswith("todo_") for n in allowed):
        parts.append(
            "The user's daily TODO list is shown in the right panel. Use todo_list to read "
            "it (each line starts with the item id), and todo_create / todo_update / "
            "todo_done / todo_delete to change it. When you add or complete items, say so "
            "briefly. If you only have todo_list, you may read but not modify it."
        )
    if any(n.startswith("workflow_") for n in allowed):
        parts.append(
            "To run a tracked multi-step process, use workflow_list / workflow_run / "
            "workflow_step; the right-panel Workflow tab shows live progress. After "
            "workflow_run (note the run_id and step ids it returns), mark each step with "
            "workflow_step(run_id, step_id, 'done') as you complete it."
        )
    skills = SkillLoader(project_slug=project_slug).build_index_prompt()
    if skills:
        parts.append("\n" + skills)
    mem = read_agent_memory(agent.id) if agent else ""
    if mem:
        parts.append("\nYour persistent memory (private to this agent):\n" + mem)
    # Global memory (MEMORY.md) distilled from past conversations (P2)
    from .knowledge.injection import wrap_context_section

    global_mem = _read_global_memory()
    if global_mem:
        parts.append("\n" + wrap_context_section("injected_memory", global_mem))
    # LLMWiki: retrieve vault entries relevant to the current query and inject
    # them as data (wrapped in <injected_wiki>). No-op unless knowledge is enabled.
    if query:
        from .knowledge.injection import build_wiki_context, wrap_context_section

        wiki_ctx = build_wiki_context(query)
        if wiki_ctx:
            parts.append("\n" + wrap_context_section("injected_wiki", wiki_ctx))
    return "\n".join(parts)


def _latest_human_text(messages) -> str:
    """The most recent user message text — used as the wiki retrieval query."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            c = getattr(m, "content", "")
            return c if isinstance(c, str) else ""
    return ""


def _read_global_memory() -> str:
    """Read global MEMORY.md (distilled from past conversations)."""
    from . import paths

    p = paths.memory_index_path()
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    # Skip default boilerplate
    if text.startswith("# Ginno Memory"):
        return ""
    return text


def _turn_agent_id(state: AgentState, config) -> str | None:
    # config['configurable'] is injected reliably every step (including after a
    # permission interrupt resume), unlike the input dict on a continued thread.
    cfg = (config or {}).get("configurable") or {}
    return cfg.get("agent_id") or state.get("agent_id")


def agent_node_factory(model, all_tools):
    async def agent_node(state: AgentState, config=None) -> dict:
        agent = _resolve_agent(_turn_agent_id(state, config))
        allowed = [t for t in all_tools if tool_allowed(agent, t.name)]
        bound = (
            model.bind_tools(allowed)
            if allowed and hasattr(model, "bind_tools")
            else model
        )
        sys_msg = SystemMessage(
            content=build_agent_system_prompt(
                agent,
                state.get("project_slug", ""),
                all_tools,
                query=_latest_human_text(state.get("messages", [])),
            )
        )
        history = [m for m in state.get("messages", []) if not isinstance(m, SystemMessage)]
        response = await bound.ainvoke([sys_msg] + history)
        tool_calls = getattr(response, "tool_calls", None) or []
        return {"messages": [response], "pending_tool_calls": tool_calls}

    return agent_node


def permission_node_factory(policy: PermissionPolicy, hook_dispatcher, all_tools):
    async def permission_node(state: AgentState, config=None) -> Command:
        from .hooks.dispatcher import HookEvent

        agent = _resolve_agent(_turn_agent_id(state, config))
        pending = state.get("pending_tool_calls") or []
        for tc in pending:
            name = tc.get("name", "")
            args = tc.get("args", {})

            # structured-output tools never need permission / hooks
            if name in RENDER_TOOL_NAMES:
                continue

            # 0) per-agent tools_allow enforcement
            if not tool_allowed(agent, name):
                return Command(
                    goto="agent",
                    update={
                        "messages": [
                            AIMessage(
                                content=(
                                    f"{BLOCK_PREFIX}{name}] {name} 不可用于 "
                                    f"{agent.name if agent else 'this agent'}。"
                                    "请改用你可用的工具，或直接回答。"
                                )
                            )
                        ]
                    },
                )

            # TODO tools: an agent that has them (per tools_allow) never needs a prompt
            if name in TODO_TOOL_NAMES:
                continue
            # workflow / artifact tools never need a prompt either
            if name in WORKFLOW_TOOL_NAMES or name in ARTIFACT_TOOL_NAMES:
                continue

            # 1) PreToolUse hooks
            if hook_dispatcher:
                results = await hook_dispatcher.dispatch(
                    HookEvent(name="PreToolUse", context={"tool": name, "args": args}),
                    matcher=name,
                )
                for r in results:
                    if r.block:
                        return Command(
                            goto="agent",
                            update={
                                "messages": [
                                    AIMessage(content=f"{BLOCK_PREFIX}{name}] hook blocked: {r.reason}")
                                ]
                            },
                        )

            # 2) permission policy
            decision = policy.decide(name, repr(args))
            if decision == "ask":
                answer = interrupt({"kind": "permission_request", "tool": name, "args": args})
                if answer.get("decision") == "deny":
                    return Command(
                        goto="agent",
                        update={
                            "messages": [AIMessage(content=f"{BLOCK_PREFIX}{name}] user denied")]
                        },
                    )
            elif decision == "deny":
                return Command(
                    goto="agent",
                    update={
                        "messages": [AIMessage(content=f"{BLOCK_PREFIX}{name}] policy denied")]
                    },
                )
        return Command(goto="tools")

    return permission_node


def route_after_agent(state: AgentState) -> Literal["permission", "__end__"]:
    return "permission" if state.get("pending_tool_calls") else END


def build_graph(
    model,
    project_slug: str,
    workspace: str,
    mcp_tools: list | None = None,
    hook_dispatcher=None,
):
    """Compose the main agent graph (single graph, union toolset)."""
    all_tools = (
        build_builtin_tools()
        + (mcp_tools or [])
        + [render_widget, attach_ref]
        + ALL_TODO_TOOLS
        + ALL_WORKFLOW_TOOLS
        + ALL_ARTIFACT_TOOLS
    )
    policy = PermissionPolicy.from_settings()

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node_factory(model, all_tools))
    g.add_node("permission", permission_node_factory(policy, hook_dispatcher, all_tools))
    g.add_node("tools", ToolNode(all_tools))

    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"permission": "permission", END: END})
    g.add_edge("permission", "tools")
    g.add_edge("tools", "agent")

    return g.compile(checkpointer=FileCheckpointer(project_slug=project_slug))
