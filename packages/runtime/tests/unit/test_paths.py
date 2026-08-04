"""Unit tests for path resolution, layout seeding, and settings migration."""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths

pytestmark = pytest.mark.unit


def test_home_honors_ginno_home_env(isolated_home):
    # isolated_home sets $GINNO_HOME to the tmp path
    assert paths.home() == isolated_home.resolve()


def test_home_falls_back_to_dot_ginno(monkeypatch):
    monkeypatch.delenv("GINNO_HOME", raising=False)
    assert paths.home().name == ".ginno"


def test_ensure_layout_creates_tree_and_defaults(seeded_home):
    for sub in ("memory", "projects", "skills", "mcp", "agents", "workflows", "logs"):
        assert (seeded_home / sub).is_dir()
    assert (seeded_home / "settings.json").is_file()
    assert (seeded_home / "config.json").is_file()
    assert (seeded_home / "MEMORY.md").is_file()
    assert (seeded_home / "mcp" / "mcp.json").is_file()


def test_ensure_layout_seeds_default_permissions(seeded_home):
    settings = json.loads((seeded_home / "settings.json").read_text())
    perms = settings["permissions"]
    assert "read_file" in perms["allow"]
    assert "Bash(rm -rf *)" in perms["deny"]
    assert "write_file" in perms["ask"]


def test_ensure_layout_seeds_default_agents(seeded_home):
    ids = {p.stem for p in (seeded_home / "agents").glob("*.json")}
    assert {"dev", "research", "writer"} <= ids


def test_settings_migration_anthropic(seeded_home):
    settings_file = seeded_home / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "model": {"provider": "anthropic", "name": "claude-x"},
                "env": {"ANTHROPIC_API_KEY": "sk-ant"},
            }
        )
    )
    paths.ensure_layout()
    migrated = json.loads(settings_file.read_text())
    assert migrated["providers"]["anthropic"]["enabled"] is True
    assert migrated["providers"]["anthropic"]["api_key"] == "sk-ant"
    assert migrated["providers"]["anthropic"]["default_model"] == "claude-x"
    assert migrated["default_provider"] == "anthropic"


def test_settings_migration_openai_compatible_becomes_custom(seeded_home):
    settings_file = seeded_home / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "model": {"provider": "openai", "name": "qwen"},
                "env": {"OPENAI_API_KEY": "sk-x", "OPENAI_BASE_URL": "https://example/v1"},
            }
        )
    )
    paths.ensure_layout()
    migrated = json.loads(settings_file.read_text())
    # an explicit base_url means an OpenAI-compatible endpoint -> custom card
    assert migrated["providers"]["custom"]["enabled"] is True
    assert migrated["providers"]["custom"]["base_url"] == "https://example/v1"
    assert migrated["default_provider"] == "custom"


def test_existing_providers_not_clobbered(seeded_home):
    settings_file = seeded_home / "settings.json"
    settings_file.write_text(
        json.dumps({"providers": {"custom": {"enabled": True, "api_key": "keep-me"}}})
    )
    paths.ensure_layout()
    after = json.loads(settings_file.read_text())
    assert after["providers"]["custom"]["api_key"] == "keep-me"


def test_session_dirs_layout(isolated_home):
    d = paths.session_files_dir("default", "sid123")
    assert d == paths.project_sessions_dir("default") / "sid123"
    assert paths.session_uploads_dir("default", "sid123") == d / "uploads"
    assert paths.session_results_dir("default", "sid123") == d / "results"


def test_session_dir_coexists_with_checkpoint_file(isolated_home):
    # checkpoint is sessions/<sid>.json (file); session files are sessions/<sid>/ (dir)
    sess_dir = paths.project_sessions_dir("default")
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "sid123.json").write_text("[]")  # checkpoint file
    fdir = paths.session_files_dir("default", "sid123")
    fdir.mkdir(parents=True, exist_ok=True)  # same base name, different type
    assert (sess_dir / "sid123.json").is_file()
    assert fdir.is_dir()
    # a glob cleanup like `sessions/sid123*` would hit BOTH — the accessor targets
    # the exact .json instead, so deleting the checkpoint leaves the dir intact
    (sess_dir / "sid123.json").unlink()
    assert fdir.is_dir()
