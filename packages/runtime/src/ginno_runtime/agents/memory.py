"""Per-agent memory helpers.

Each agent's long-term memory lives under ~/.ginno/agents/<id>/ with a
MEMORY.md index and a memory/ folder of entries — mirroring the global
memory layout but scoped to the persona, so agents keep independent
memories (decision #1).
"""

from __future__ import annotations

from pathlib import Path

from .. import paths

_INDEX = "# {name} Memory\n\n{seed}\n\nSee [memory/](./memory/) for entries.\n"


def agent_memory_dir(agent_id: str) -> Path:
    return paths.agents_dir() / agent_id / "memory"


def ensure_agent_memory(agent_id: str, name: str, seed: str = "") -> Path:
    d = agent_memory_dir(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    idx = paths.agents_dir() / agent_id / "MEMORY.md"
    if not idx.exists():
        idx.write_text(_INDEX.format(name=name, seed=seed or f"Long-term memory for {name}."))
    return d


def read_agent_memory(agent_id: str | None) -> str:
    """Return the agent's MEMORY.md text (+ entry filenames) for prompt injection."""
    if not agent_id:
        return ""
    base = paths.agents_dir() / agent_id
    parts: list[str] = []
    idx = base / "MEMORY.md"
    if idx.exists():
        t = idx.read_text(encoding="utf-8").strip()
        if t:
            parts.append(t)
    md = base / "memory"
    if md.exists():
        names = sorted(p.name for p in md.glob("*.md"))
        if names:
            parts.append("memory entries: " + ", ".join(names))
    return "\n".join(parts)
