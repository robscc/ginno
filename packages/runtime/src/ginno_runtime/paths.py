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


def ensure_layout() -> None:
    """Create the standard ~/.ginno directory tree if missing."""
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
    for f in ("settings.json", "config.json", "MEMORY.md", "mcp/mcp.json"):
        p = root / f
        if not p.exists():
            p.touch()


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
