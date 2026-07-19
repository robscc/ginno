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
        tools_allow=["read_file", "glob_files", "grep_files", "mcp_*"],
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
        tools_allow=["read_file", "write_file", "edit_file", "glob_files", "mcp_*"],
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
