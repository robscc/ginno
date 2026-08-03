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
    # Privileged mode ON by default: allow every tool call without permission
    # prompts (toggle off in Settings → 通用 to re-enable per-tool checks).
    "bypass_permissions": True,
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
        "allow": ["Read(*)", "Glob(*)", "Grep(*)", "read_file", "glob_files", "grep_files", "parse_document", "mcp_vault_read_*", "mcp_vault_list_*", "mcp_vault_search_*", "mcp_vault_directory_*", "mcp_vault_get_*"],
        "deny": ["Bash(rm -rf *)", "bash(rm -rf *)", "Bash(sudo *)", "bash(sudo *)", "Write(~/.ssh/**)", "Write(~/.gnupg/**)"],
        "ask": ["Bash(*)", "bash(*)", "Write(*)", "Edit(*)", "write_file", "edit_file", "analyze_table", "mcp_vault_write_*", "mcp_vault_edit_*", "mcp_vault_create_*", "mcp_vault_move_*"],
    },
    "hooks": {},
    "knowledge": {
        "enabled": False,
        "vault_path": "",
        "raw_dir": "Ginno/Raw",
        "wiki_dir": "Ginno/Wiki",
        "research_dir": "Ginno/Research",
        "auto_inject": True,
        "inject_top_k": 5,
        "inject_min_score": 0.3,
        "rescan_interval_s": 60,
        "use_semantic": False,
        "capture": True,
        "auto_summarize": True,
        "pool_flush_threshold": 30,
        "summarize_model": "",
        "memory_budget_chars": 3000,
    },
}

# Default MCP servers shipped with Ginno. Playwright gives every agent the ability
# to operate a real (headless) browser — navigate / screenshot / snapshot — which
# powers web end-to-end flows (e.g. open GitHub Trending, screenshot, analyze).
# `--browser chrome` reuses an installed Google Chrome so no Chromium download is
# needed; falls back gracefully (server skipped) when no browser is available.
_DEFAULT_MCP = {
    "mcpServers": {
        "playwright": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest", "--headless", "--browser", "chrome"],
            # first-run `npx` install + headless Chrome launch can exceed the
            # default 15s; give the default browser MCP room to come up.
            "connect_timeout": 60,
        }
    }
}

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
        "knowledge",
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


def files_index_path(slug: str) -> Path:
    return project_dir(slug) / "files.json"


def session_index_path(slug: str) -> Path:
    return project_sessions_dir(slug) / "_index.json"


# ---- knowledge / LLMWiki + memory refinery ----
def knowledge_dir() -> Path:
    return home() / "knowledge"


def memory_pool_dir() -> Path:
    return home() / "memory" / "pool"


def experiences_path() -> Path:
    return home() / "experiences.jsonl"


def watermarks_path() -> Path:
    return home() / "watermarks.json"
