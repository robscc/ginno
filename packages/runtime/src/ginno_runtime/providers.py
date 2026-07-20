"""Model provider registry — multi-provider settings + connectivity verify.

settings.json shape (under `providers`):

    {
      "default_provider": "custom",
      "providers": {
        "anthropic": {enabled, protocol, api_key, default_model, base_url,
                      max_tokens, temperature, timeout_s},
        "openai":    {enabled, protocol, api_key, default_model, base_url,
                      org_id, max_tokens},
        "custom":    {enabled, protocol:"openai-compatible", name, api_key,
                      base_url, model, max_tokens, temperature, timeout_s}
      }
    }

Reads merge stored values over PROVIDER_DEFAULTS so newly-added fields
always have a sane value. Old single-`model`+`env` settings are migrated
by paths.ensure_layout() before this module sees them.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

from . import paths

PROVIDER_IDS = ("anthropic", "openai", "custom")

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
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
}


def _read_settings() -> dict[str, Any]:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _write_settings(settings: dict[str, Any]) -> None:
    paths.settings_path().write_text(
        json.dumps(settings, indent=2, ensure_ascii=False)
    )


def load_providers(settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return the providers map with defaults merged in for every known id."""
    settings = settings if settings is not None else _read_settings()
    stored = settings.get("providers", {}) or {}
    out: dict[str, dict[str, Any]] = {}
    for pid in PROVIDER_IDS:
        merged = deepcopy(PROVIDER_DEFAULTS[pid])
        merged.update(stored.get(pid, {}) or {})
        out[pid] = merged
    # preserve any extra custom ids the user added beyond the builtin three
    for pid, cfg in stored.items():
        if pid not in out:
            out[pid] = cfg
    return out


def save_providers(providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    settings = _read_settings()
    # merge over defaults so partial writes still yield full records
    normalized: dict[str, dict[str, Any]] = {}
    for pid, cfg in providers.items():
        base = deepcopy(PROVIDER_DEFAULTS.get(pid, {}))
        base.update(cfg or {})
        normalized[pid] = base
    settings["providers"] = normalized
    _write_settings(settings)
    return normalized


def get_default_provider(settings: dict[str, Any] | None = None) -> str:
    settings = settings if settings is not None else _read_settings()
    providers = load_providers(settings)
    chosen = settings.get("default_provider")
    if chosen in providers and providers[chosen].get("enabled"):
        return chosen
    for pid in PROVIDER_IDS:
        if providers[pid].get("enabled"):
            return pid
    # nothing enabled — fall back to default_provider anyway (will surface as error later)
    return chosen or "custom"


def model_for_provider(providers: dict[str, dict[str, Any]], pid: str) -> str:
    cfg = providers.get(pid, {})
    return cfg.get("default_model") or cfg.get("model") or ""


def verify(provider_id: str) -> dict[str, Any]:
    """Probe a provider with the cheapest possible call. Never raises."""
    providers = load_providers()
    cfg = providers.get(provider_id)
    if not cfg:
        return {"ok": False, "error": f"unknown provider: {provider_id}"}
    proto = cfg.get("protocol")
    timeout = float(cfg.get("timeout_s") or 60)
    t0 = time.time()

    def _latency() -> int:
        return int((time.time() - t0) * 1000)

    try:
        if proto == "anthropic":
            if not cfg.get("api_key"):
                return {"ok": False, "error": "API Key 为空", "latency_ms": _latency()}
            import anthropic

            client_kw: dict[str, Any] = {
                "base_url": cfg.get("base_url") or None,
                "timeout": timeout,
            }
            if cfg.get("bearer_auth"):
                client_kw["auth_token"] = cfg["api_key"]  # Authorization: Bearer
            else:
                client_kw["api_key"] = cfg["api_key"]  # x-api-key
            client = anthropic.Anthropic(**client_kw)
            client.messages.create(
                model=model_for_provider(providers, provider_id) or "claude-3-7-sonnet-20250219",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return {"ok": True, "latency_ms": _latency()}

        # openai / openai-compatible
        import openai

        base = cfg.get("base_url") or None
        if proto == "openai" and not base:
            base = "https://api.openai.com/v1"
        client = openai.OpenAI(
            api_key=cfg.get("api_key") or "not-needed",
            base_url=base,
            timeout=timeout,
        )
        try:
            client.models.list()
        except Exception:
            # some compatible endpoints lack /models — fall back to a 1-token chat
            client.chat.completions.create(
                model=model_for_provider(providers, provider_id) or "gpt-4o",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        return {"ok": True, "latency_ms": _latency()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": _latency()}
