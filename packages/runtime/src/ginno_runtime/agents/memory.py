"""Per-agent memory helpers.

When an agent's `memory_scope` is "agent", its long-term memory lives under
~/.ginno/agents/<id>/memory/ with a MEMORY.md index — mirroring the global
memory layout but scoped to the persona.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths

_INDEX = "# {name} Memory\n\nLong-term memory for this agent. See [memory/](./memory/).\n"


def agent_memory_dir(agent_id: str) -> Path:
    return paths.agents_dir() / agent_id / "memory"


def ensure_agent_memory(agent_id: str, name: str) -> Path:
    d = agent_memory_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    idx = paths.agents_dir() / agent_id / "MEMORY.md"
    if not idx.exists():
        idx.write_text(_INDEX.format(name=name))
    return d
