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
    "default_provider": "custom",
    "providers": {
        "anthropic": {
            "enabled": False,
            "protocol": "anthropic",
            "api_key": "",
            "default_model": "claude-3-7-sonnet-20250219",
            "base_url": "",
            "max_tokens": 4096,
            "temperature": 0.7,
            "timeout_s": 60,
        },
        "openai": {
            "enabled": False,
            "protocol": "openai",
            "api_key": "",
            "default_model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
            "org_id": "",
            "max_tokens": 8192,
        },
        "custom": {
            "enabled": False,
            "protocol": "openai-compatible",
            "name": "",
            "api_key": "",
            "base_url": "",
            "model": "",
            "max_tokens": 8192,
            "temperature": 0.7,
            "timeout_s": 60,
        },
    },
    "permissions": {
        "allow": ["Read(*)", "Glob(*)", "Grep(*)", "read_file", "glob_files", "grep_files", "mcp_vault_read_*", "mcp_vault_list_*", "mcp_vault_search_*", "mcp_vault_directory_*", "mcp_vault_get_*"],
        "deny": ["Bash(rm -rf *)", "bash(rm -rf *)", "Bash(sudo *)", "bash(sudo *)", "Write(~/.ssh/**)", "Write(~/.gnupg/**)"],
        "ask": ["Bash(*)", "bash(*)", "Write(*)", "Edit(*)", "write_file", "edit_file", "mcp_vault_write_*", "mcp_vault_edit_*", "mcp_vault_create_*", "mcp_vault_move_*"],
    },
    "hooks": {},
}

_DEFAULT_MCP = {"mcpServers": {}}

_DEFAULT_MEMORY_INDEX = "# Ginno Memory\n\nLong-term memory entries. See [memory/](./memory/).\n"


def _migrate_settings(settings: dict) -> dict:
    """Upgrade an old single-`model`+`env` settings file to the providers shape.

    Only fills `providers` when it is absent, so the user's existing key/base_url
    (e.g. an OpenAI-compatible endpoint) is preserved instead of clobbered.
    """
    from copy import deepcopy

    if settings.get("providers"):
        return settings  # already new shape

    from . import providers as prov_mod  # local import: avoid cycle at module load

    provs = deepcopy(prov_mod.PROVIDER_DEFAULTS)
    old_model = settings.get("model", {}) or {}
    env = settings.get("env", {}) or {}
    mp = old_model.get("provider")
    mn = old_model.get("name")
    default = "custom"

    if mp == "anthropic":
        p = provs["anthropic"]
        p["enabled"] = True
        p["api_key"] = env.get("ANTHROPIC_API_KEY", "")
        if mn:
            p["default_model"] = mn
        default = "anthropic"
    elif mp == "openai":
        base = env.get("OPENAI_BASE_URL", "")
        if base:  # an explicit base_url means OpenAI-compatible → custom card
            p = provs["custom"]
            p["enabled"] = True
            p["api_key"] = env.get("OPENAI_API_KEY", "")
            p["base_url"] = base
            p["model"] = mn or ""
            p["name"] = p["name"] or "Migrated OpenAI-Compatible"
            default = "custom"
        else:
            p = provs["openai"]
            p["enabled"] = True
            p["api_key"] = env.get("OPENAI_API_KEY", "")
            if mn:
                p["default_model"] = mn
            default = "openai"

    settings["providers"] = provs
    settings.setdefault("default_provider", default)
    return settings


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
        "agents",
        "workflows",
    ):
        (root / sub).mkdir(parents=True, exist_ok=True)

    settings = root / "settings.json"
    if not settings.exists() or settings.stat().st_size == 0:
        settings.write_text(json.dumps(_DEFAULT_SETTINGS, indent=2, ensure_ascii=False))
    else:
        # migrate old shape in place (preserves user keys)
        try:
            data = json.loads(settings.read_text() or "{}")
            had_providers = "providers" in data
            _migrate_settings(data)
            if not had_providers:
                settings.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        except json.JSONDecodeError:
            pass

    config = root / "config.json"
    if not config.exists() or config.stat().st_size == 0:
        config.write_text(json.dumps({"theme": "dark"}, indent=2, ensure_ascii=False))

    mem = root / "MEMORY.md"
    if not mem.exists() or mem.stat().st_size == 0:
        mem.write_text(_DEFAULT_MEMORY_INDEX)

    mcp = root / "mcp" / "mcp.json"
    if not mcp.exists() or mcp.stat().st_size == 0:
        mcp.write_text(json.dumps(_DEFAULT_MCP, indent=2, ensure_ascii=False))

    todos = root / "todos.json"
    if not todos.exists():
        todos.write_text(json.dumps([], indent=2, ensure_ascii=False))

    # seed default agents (dev / research / writer)
    from .agents.registry import ensure_seeded  # local import: paths is imported by registry

    ensure_seeded()


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


def agents_dir() -> Path:
    return home() / "agents"


def todos_path() -> Path:
    return home() / "todos.json"


def workflows_dir() -> Path:
    return home() / "workflows"


def workflow_runs_dir(slug: str) -> Path:
    return project_dir(slug) / "workflow_runs"


def artifacts_path(slug: str) -> Path:
    return project_dir(slug) / "artifacts.json"


def session_index_path(slug: str) -> Path:
    return project_sessions_dir(slug) / "_index.json"
