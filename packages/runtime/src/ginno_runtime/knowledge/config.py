"""Read the `knowledge` config block from settings.json (defaults merged in)."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from ginno_runtime import jsonx as json

from .. import settings_store
from .types import KnowledgeConfig


def _read_settings() -> dict[str, Any]:
    return settings_store.read_settings()


def load_knowledge_config(settings: dict[str, Any] | None = None) -> KnowledgeConfig:
    """Return a KnowledgeConfig with stored values merged over dataclass defaults."""
    settings = settings if settings is not None else _read_settings()
    stored = settings.get("knowledge", {}) or {}
    known = {f.name for f in fields(KnowledgeConfig)}
    kwargs = {k: v for k, v in stored.items() if k in known}
    return KnowledgeConfig(**kwargs)


def save_knowledge_config(cfg: KnowledgeConfig) -> None:
    """Persist the knowledge block back into settings.json (锁内读-改-写，
    保留其它键；损坏拒写保留原字节)."""
    from dataclasses import asdict

    def _mut(settings: dict[str, Any]) -> dict[str, Any]:
        settings["knowledge"] = asdict(cfg)
        return settings

    settings_store.update(_mut)
