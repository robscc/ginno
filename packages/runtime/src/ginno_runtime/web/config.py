"""The `web` settings block (search engines + defaults)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .. import paths


@dataclass
class WebConfig:
    enabled: bool = True
    default_engine: str = "duckduckgo"
    engines: dict[str, dict] = field(default_factory=dict)
    max_results: int = 5
    timeout_s: int = 15

    def engine_cfg(self, name: str) -> dict:
        return dict((self.engines or {}).get(name) or {})


def load_web_config(settings: dict[str, Any] | None = None) -> WebConfig:
    if settings is None:
        p = paths.settings_path()
        try:
            settings = json.loads(p.read_text() or "{}") if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            settings = {}
    stored = settings.get("web", {}) or {}
    known = ("enabled", "default_engine", "engines", "max_results", "timeout_s")
    kwargs = {k: stored[k] for k in known if k in stored}
    return WebConfig(**kwargs)
