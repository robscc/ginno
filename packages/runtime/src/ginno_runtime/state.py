"""LangGraph state schema for the main agent graph."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    workspace: str
    project_slug: str
    agent_id: str
    active_skills: list[str]
    pending_tool_calls: list[dict]
    # Files attached to the current turn (uploaded or path-referenced). Each
    # item: {id, name, path, kind, schema?}. Injected into the system prompt
    # and surfaced as file blocks in the UI history.
    attached_files: list[dict]
    # Resolved @mention context for the current turn (workflow / memory /
    # non-file artifact). Each item: {kind, id, name, summary}. Injected as a
    # per-turn context message (plan B1). Reset to [] every turn by the WS
    # layer (no reducer — last value wins, like attached_files).
    mention_context: list[dict]
    # MCP tool names for this session — feeds the WorldState `mcp` section so
    # a mid-session MCP reload can be detected and announced (plan A7). Set by
    # the WS layer every invoke; persists across steps like other channels.
    mcp_tool_names: list[str]
