"""Read the `knowledge` config block from settings.json (defaults merged in)."""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any

from .. import paths
from .types import KnowledgeConfig


def _read_settings() -> dict[str, Any]:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def load_knowledge_config(settings: dict[str, Any] | None = None) -> KnowledgeConfig:
    """Return a KnowledgeConfig with stored values merged over dataclass defaults."""
    settings = settings if settings is not None else _read_settings()
    stored = settings.get("knowledge", {}) or {}
    known = {f.name for f in fields(KnowledgeConfig)}
    kwargs = {k: v for k, v in stored.items() if k in known}
    return KnowledgeConfig(**kwargs)


def save_knowledge_config(cfg: KnowledgeConfig) -> None:
    """Persist the knowledge block back into settings.json (preserving other keys)."""
    from dataclasses import asdict

    settings = _read_settings()
    settings["knowledge"] = asdict(cfg)
    paths.settings_path().write_text(json.dumps(settings, indent=2, ensure_ascii=False))
