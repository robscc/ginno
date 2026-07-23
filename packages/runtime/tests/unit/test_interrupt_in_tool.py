"""Isolation test: does an interrupt() raised *inside a tool* (via ToolNode)
surface as an __interrupt__ in graph.astream updates mode? This is the mechanism
P5's workflow_propose_edit relies on. If this fails, the interrupt must move out
of the tool body into a graph node."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from ginno_runtime.state import AgentState


@tool
def pause_tool(x: str = "") -> str:
    """Pause for a decision then echo it."""
    decision = interrupt({"kind": "version_propose", "x": x})
    return f"resumed:{decision}"


def _build():
    g = StateGraph(AgentState)
    g.add_node("tools", ToolNode([pause_tool]))
    g.add_edge(START, "tools")
    g.add_edge("tools", END)
    return g.compile(checkpointer=InMemorySaver())


@pytest.mark.asyncio
async def test_interrupt_inside_tool_surfaces():
    graph = _build()
    seed = AIMessage(
        content="",
        tool_calls=[{"name": "pause_tool", "args": {"x": "hi"}, "id": "c1", "type": "tool_call"}],
    )
    state = {
        "messages": [HumanMessage(content="go"), seed],
        "workspace": "/tmp",
        "project_slug": "default",
        "agent_id": "dev",
        "active_skills": [],
        "pending_tool_calls": [],
    }
    saw_interrupt = None
    async for _mode, payload in graph.astream(
        state, config={"configurable": {"thread_id": "t1"}}, stream_mode=["updates"]
    ):
        if "__interrupt__" in (payload or {}):
            intrs = payload["__interrupt__"]
            saw_interrupt = getattr(intrs[0], "value", None)
    assert saw_interrupt is not None, "interrupt() inside tool did not surface"
    assert saw_interrupt.get("kind") == "version_propose"

    # resume and confirm the tool completes
    from langgraph.types import Command

    out = []
    async for _mode, payload in graph.astream(
        Command(resume="allow"),
        config={"configurable": {"thread_id": "t1"}},
        stream_mode=["updates"],
    ):
        out.append(payload)
    tool_msgs = [m for p in out for m in (p.get("tools") or {}).get("messages", [])]
    assert any("resumed:allow" in getattr(m, "content", "") for m in tool_msgs), out
