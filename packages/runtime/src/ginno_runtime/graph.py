"""Main LangGraph: load_context → agent → permission → tools → agent (loop).

P0 scaffold: wires the topology with `interrupt()` for HITL on permission
`ask` decisions, plus the file checkpointer. Model binding + real LLM
call lands in P1.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from .checkpointer import FileCheckpointer
from .permission.policy import PermissionPolicy
from .skills.loader import SkillLoader
from .state import AgentState
from .tools.builtin import build_builtin_tools


def _build_system_prompt(workspace: str, skills_prompt: str) -> str:
    parts = [
        "You are Ginno, a personal AI agent.",
        f"Workspace: {workspace}",
        "You have tools: Read/Write/Edit/Glob/Grep/Bash + MCP tools.",
        "Ask the user via permission prompts before destructive operations.",
    ]
    if skills_prompt:
        parts.append(skills_prompt)
    return "\n".join(parts)


def load_context_node(state: AgentState) -> dict:
    """Inject system prompt + skills index + workspace context."""
    skills_prompt = SkillLoader(project_slug=state["project_slug"]).build_index_prompt()
    sys_msg = SystemMessage(content=_build_system_prompt(state["workspace"], skills_prompt))
    # prepend system message if not present
    if not state.get("messages"):
        return {"messages": [sys_msg]}
    return {}


def agent_node_factory(model, tools):
    """Return a node function that calls the model bound with tools."""

    bound = model.bind_tools(tools) if hasattr(model, "bind_tools") else model

    async def agent_node(state: AgentState) -> dict:
        response = await bound.ainvoke(state["messages"])
        tool_calls = getattr(response, "tool_calls", None) or []
        return {
            "messages": [response],
            "pending_tool_calls": tool_calls,
        }

    return agent_node


def permission_node_factory(policy: PermissionPolicy):
    async def permission_node(state: AgentState) -> Command:
        pending = state.get("pending_tool_calls") or []
        for tc in pending:
            name = tc.get("name", "")
            args_repr = repr(tc.get("args", {}))
            decision = policy.decide(name, args_repr)
            if decision == "ask":
                answer = interrupt(
                    {
                        "kind": "permission_request",
                        "tool": name,
                        "args": tc.get("args", {}),
                    }
                )
                if answer.get("decision") == "deny":
                    return Command(
                        goto="agent",
                        update={
                            "messages": [
                                AIMessage(content=f"[user denied tool: {name}]")
                            ],
                            "pending_tool_calls": [],
                        },
                    )
            elif decision == "deny":
                return Command(
                    goto="agent",
                    update={
                        "messages": [AIMessage(content=f"[policy blocked tool: {name}]")],
                        "pending_tool_calls": [],
                    },
                )
        # all allowed — fall through to tools
        return Command(goto="tools")

    return permission_node


def route_after_agent(state: AgentState) -> Literal["permission", "__end__"]:
    if state.get("pending_tool_calls"):
        return "permission"
    return END


def build_graph(model, project_slug: str, workspace: str, mcp_tools: list | None = None):
    """Compose the main agent graph. Returns a compiled CompiledStateGraph."""
    tools = build_builtin_tools() + (mcp_tools or [])
    policy = PermissionPolicy.from_settings()

    g = StateGraph(AgentState)
    g.add_node("load_context", load_context_node)
    g.add_node("agent", agent_node_factory(model, tools))
    g.add_node("permission", permission_node_factory(policy))
    g.add_node("tools", ToolNode(tools))

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "agent")
    g.add_conditional_edges("agent", route_after_agent, {"permission": "permission", END: END})
    g.add_edge("permission", "tools")  # only reached when allow; ask/deny go back to agent via Command
    g.add_edge("tools", "agent")

    checkpointer = FileCheckpointer(project_slug=project_slug)
    return g.compile(checkpointer=checkpointer)
