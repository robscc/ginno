"""Resolve Ginno paths under ~/.ginno.

Mirrors the Claude Code ~/.claude layout: user project files live in
~/workspace/<proj>, agent metadata lives under ~/.ginno/projects/<slug>.
"""

from __future__ import annotations

import os
from pathlib import Path

_GINNO_HOME_ENV = "GINNO_HOME"


def home() -> Path:
    """Root of all Ginno state. Override with $GINNO_HOME for tests."""
    override = os.environ.get(_GINNO_HOME_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".ginno"


_DEFAULT_SETTINGS = {
    "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
    "env": {},
    "permissions": {
        "allow": ["Read(*)", "Glob(*)", "Grep(*)"],
        "deny": ["Bash(rm -rf *)", "Bash(sudo *)", "Write(~/.ssh/**)", "Write(~/.gnupg/**)"],
        "ask": ["Bash(*)", "Write(*)", "Edit(*)"],
    },
    "hooks": {},
}

_DEFAULT_MCP = {"mcpServers": {}}

_DEFAULT_MEMORY_INDEX = "# Ginno Memory\n\nLong-term memory entries. See [memory/](./memory/).\n"


def ensure_layout() -> None:
    """Create the standard ~/.ginno directory tree with seed defaults."""
    import json

    root = home()
    for sub in (
        "memory",
        "projects",
        "skills",
        "mcp",
        "hooks",
        "vectorstore",
        "logs",
        "cache",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    settings = root / "settings.json"
    if not settings.exists() or settings.stat().st_size == 0:
        settings.write_text(json.dumps(_DEFAULT_SETTINGS, indent=2, ensure_ascii=False))

    config = root / "config.json"
    if not config.exists() or config.stat().st_size == 0:
        config.write_text(json.dumps({"theme": "system"}, indent=2, ensure_ascii=False))

    mem = root / "MEMORY.md"
    if not mem.exists() or mem.stat().st_size == 0:
        mem.write_text(_DEFAULT_MEMORY_INDEX)

    mcp = root / "mcp" / "mcp.json"
    if not mcp.exists() or mcp.stat().st_size == 0:
        mcp.write_text(json.dumps(_DEFAULT_MCP, indent=2, ensure_ascii=False))


def project_dir(slug: str) -> Path:
    """Agent metadata dir for a project slug."""
    return home() / "projects" / slug


def project_sessions_dir(slug: str) -> Path:
    return project_dir(slug) / "sessions"


def project_skills_dir(slug: str) -> Path:
    return project_dir(slug) / "skills"


def global_skills_dir() -> Path:
    return home() / "skills"


def mcp_config_path() -> Path:
    return home() / "mcp" / "mcp.json"


def settings_path() -> Path:
    return home() / "settings.json"


def memory_index_path() -> Path:
    return home() / "MEMORY.md"


def memory_dir() -> Path:
    return home() / "memory"
