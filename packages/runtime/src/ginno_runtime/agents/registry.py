"""Agent registry.

Each agent is a JSON file under ~/.ginno/agents/<id>.json with its own
persona, model binding, tool allowlist, and (via memory.py) memory scope.

    {
      "id": "dev",
      "name": "Dev Agent",
      "icon": "terminal",        # lucide icon name
      "color": "blue",           # theme color key
      "system_prompt": "...",
      "provider": "custom",      # provider id from providers.py
      "model": "",               # empty → use provider default
      "tools_allow": ["*"],      # ["*"] = all; else fnmatch patterns over tool names
      "memory_scope": "agent"    # "agent" → ~/.ginno/agents/<id>/memory
    }

`status` (active/idle/running) is computed at runtime, not persisted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from .memory import ensure_agent_memory


@dataclass
class AgentConfig:
    id: str
    name: str
    icon: str = "terminal"
    color: str = "blue"
    system_prompt: str = ""
    provider: str = "custom"
    model: str = ""
    tools_allow: list[str] = field(default_factory=lambda: ["*"])
    memory_scope: str = "agent"
    status: str = "idle"  # runtime presence; default idle

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Default seed — matches the design mockup (Dev / Research / Writer).
_SEED: list[AgentConfig] = [
    AgentConfig(
        id="dev",
        name="Dev Agent",
        icon="terminal",
        color="blue",
        system_prompt=(
            "You are Dev Agent, a software-engineering assistant. You help with "
            "code, PRs, debugging, and repo operations. Prefer concrete actions "
            "using your tools. Be concise."
        ),
        provider="custom",
        tools_allow=["*"],
    ),
    AgentConfig(
        id="research",
        name="Research Agent",
        icon="search",
        color="orange",
        system_prompt=(
            "You are Research Agent. You gather and synthesize information, read "
            "docs and notes, and produce clear summaries with sources. Do not "
            "modify files unless asked."
        ),
        provider="custom",
        tools_allow=["read_file", "glob_files", "grep_files", "mcp_*", "todo_list"],
    ),
    AgentConfig(
        id="writer",
        name="Writer Agent",
        icon="pen-line",
        color="green",
        system_prompt=(
            "You are Writer Agent. You draft and edit documents, notes, and "
            "communications with a clear, polished voice."
        ),
        provider="custom",
        tools_allow=["read_file", "write_file", "edit_file", "glob_files", "mcp_*", "todo_*"],
    ),
    AgentConfig(
        id="workflow-dev",
        name="Workflow Dev Agent",
        icon="workflow",
        color="violet",
        system_prompt=(
            "You are Workflow Dev Agent. You edit ONE workflow's versioned DSL by "
            "conversation. The user's first message gives the workflow_id and the "
            "current DSL. To change it, call workflow_propose_edit(workflow_id, "
            "new_dsl_json, rationale) with the FULL proposed DSL object. Your edit "
            "then PAUSES: the user sees a unified diff and must Apply or Reject — "
            "there is no DAG editor, the diff confirmation is the gate. Only on Apply "
            "is a new immutable version created. DSL node types: step/branch/loop. A "
            "loop routes structurally (its body must NOT carry an explicit out-edge; "
            "reference the loop item via {{<as>}}). Validate your proposal: entry must "
            "be a node id, every edge endpoint must exist, branch needs cases or "
            "default, loop needs over+body+max_iters. Explain each change in rationale. "
            "Keep edits minimal and targeted."
        ),
        provider="custom",
        tools_allow=["workflow_propose_edit", "workflow_list"],
    ),
]


def _agent_path(agent_id: str) -> Path:
    return paths.agents_dir() / f"{agent_id}.json"


def _read(agent_id: str) -> AgentConfig | None:
    p = _agent_path(agent_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return None
    return AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})


def _write(cfg: AgentConfig) -> None:
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    p = _agent_path(cfg.id)
    p.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))


# Distinct seed memory so each persona's prompt (and thus behaviour) differs
# out of the box — demonstrates independent per-agent memory.
_MEMORY_SEED: dict[str, str] = {
    "dev": "Focus: code, PRs, debugging, repo ops. Prefer concrete tool actions.",
    "research": "Focus: gather & synthesize information, read docs/notes, cite sources. Avoid mutating files.",
    "writer": "Focus: draft & polish documents and messages with a clear voice.",
}


def ensure_seeded() -> None:
    """Create default agent files if the registry is empty."""
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    if any(paths.agents_dir().glob("*.json")):
        return
    for cfg in _SEED:
        _write(cfg)
        ensure_agent_memory(cfg.id, cfg.name, _MEMORY_SEED.get(cfg.id, ""))


# TODO tool patterns each persona should have (read-only for research).
_TODO_PATTERNS: dict[str, list[str]] = {
    "dev": ["todo_*"],
    "research": ["todo_list"],
    "writer": ["todo_*"],
}


def ensure_todo_tools() -> None:
    """Merge the TODO tool patterns into existing agents (idempotent migration)."""
    for cfg in list_agents():
        needed = _TODO_PATTERNS.get(cfg.id)
        if not needed:
            continue
        allow = list(cfg.tools_allow or ["*"])
        if "*" in allow:
            continue  # already all-inclusive
        added = [p for p in needed if p not in allow]
        if added:
            update_agent(cfg.id, {"tools_allow": allow + added})


def list_agents() -> list[AgentConfig]:
    ensure_seeded()
    out: list[AgentConfig] = []
    for p in sorted(paths.agents_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            continue
        out.append(
            AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
        )
    return out


def get_agent(agent_id: str) -> AgentConfig | None:
    ensure_seeded()
    return _read(agent_id)


def create_agent(data: dict[str, Any]) -> AgentConfig:
    cfg = AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
    if not cfg.id:
        raise ValueError("agent id required")
    if _agent_path(cfg.id).exists():
        raise ValueError(f"agent {cfg.id} already exists")
    _write(cfg)
    return cfg


def update_agent(agent_id: str, data: dict[str, Any]) -> AgentConfig:
    existing = _read(agent_id)
    if not existing:
        raise ValueError(f"agent {agent_id} not found")
    merged = existing.to_dict()
    merged.update({k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
    merged["id"] = agent_id  # id immutable
    cfg = AgentConfig(**merged)
    _write(cfg)
    return cfg


def delete_agent(agent_id: str) -> bool:
    p = _agent_path(agent_id)
    if p.exists():
        p.unlink()
        return True
    return False


def fork_agent(src_id: str, new_id: str, name: str | None = None) -> AgentConfig:
    """Copy an agent's persona/tools/model into a fresh id with EMPTY memory.

    Used by workflow runs (design §5.2): the fork reuses the source provider/model
    and tools_allow but starts with clean history + memory. Idempotent — if the
    fork id already exists (e.g. a rerun) the existing fork is returned.
    """
    src = _read(src_id)
    if not src:
        raise ValueError(f"source agent {src_id} not found")
    data = src.to_dict()
    data["id"] = new_id
    data["name"] = name or f"{src.name} (fork)"
    try:
        return create_agent(data)
    except ValueError:
        # already forked (id exists) — return the existing fork unchanged
        existing = _read(new_id)
        if existing:
            return existing
        raise
