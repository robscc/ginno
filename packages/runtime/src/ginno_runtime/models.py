"""Model factory — bind provider configs to LangChain chat models.

Reads API keys from env first, then from ~/.ginno/settings.json's `env`
block (Claude Code pattern). Supports anthropic / openai / ollama.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import paths


def _load_settings() -> dict[str, Any]:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _resolve_env(provider: str) -> dict[str, str]:
    """Pull env vars from settings.json `env` block; do not override os.environ."""
    settings = _load_settings()
    block = settings.get("env", {}) or {}
    out: dict[str, str] = {}
    # Apply all env vars from settings, then current os.environ takes precedence.
    for k, v in block.items():
        out[k] = str(v)
    for k, v in os.environ.items():
        out[k] = v
    return out


def build_model(provider: str, name: str):
    """Return a LangChain chat model for the given provider/name.

    Raises ValueError if the provider is unknown or no API key is set.
    """
    env = _resolve_env(provider)

    if provider == "anthropic":
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Put it in ~/.ginno/settings.json under "
                "`env.ANTHROPIC_API_KEY` or export it."
            )
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=name, api_key=key, streaming=True)

    if provider == "openai":
        key = env.get("OPENAI_API_KEY")
        if not key:
            raise ValueError(
                "OPENAI_API_KEY not set. Put it in ~/.ginno/settings.json under "
                "`env.OPENAI_API_KEY` or export it."
            )
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=name, api_key=key, streaming=True)

    if provider == "ollama":
        try:
            from langchain_ollama import ChatOllama
        except ImportError as e:
            raise ValueError(
                "Ollama support requires `pip install langchain-ollama`"
            ) from e
        return ChatOllama(model=name, base_url=env.get("OLLAMA_BASE_URL", "http://localhost:11434"))

    raise ValueError(f"unknown provider: {provider}")
