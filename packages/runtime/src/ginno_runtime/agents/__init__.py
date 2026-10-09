"""Agents subpackage — multi-persona registry with independent tools + memory."""

from .registry import (
    AgentConfig,
    create_agent,
    delete_agent,
    ensure_goal_tools,
    ensure_research_discipline,
    ensure_todo_tools,
    ensure_web_tools,
    ensure_workflow_dev_tools,
    ensure_workflow_dev,
    fork_agent,
    get_agent,
    list_agents,
    set_agent_order,
    update_agent,
)

__all__ = [
    "AgentConfig",
    "list_agents",
    "get_agent",
    "create_agent",
    "update_agent",
    "delete_agent",
    "fork_agent",
    "set_agent_order",
    "ensure_todo_tools",
    "ensure_research_discipline",
    "ensure_goal_tools",
    "ensure_web_tools",
    "ensure_workflow_dev_tools",
    "ensure_workflow_dev",
]
