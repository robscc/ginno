"""LangGraph state schema for the main agent graph."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    workspace: str
    project_slug: str
    active_skills: list[str]
    pending_tool_calls: list[dict]
