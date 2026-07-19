"""Model factory — build a LangChain chat model from a provider id.

Reads provider config from settings.json `providers` (see providers.py).
Sampling params: temperature as a top-level kwarg; max_tokens via
model_kwargs (robust across langchain versions). base_url + api_key as
top-level kwargs (verified present on ChatAnthropic + ChatOpenAI).
"""

from __future__ import annotations

from typing import Any

from . import providers as prov_mod


def _sampling(cfg: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    temperature = cfg.get("temperature")
    model_kwargs: dict[str, Any] = {}
    mt = cfg.get("max_tokens")
    if mt:
        model_kwargs["max_tokens"] = int(mt)
    return (float(temperature) if temperature is not None else None, model_kwargs)


def build_model(provider_id: str, model_name: str | None = None):
    """Return a LangChain chat model for the given provider id.

    `model_name` overrides the provider's configured default/model.
    Raises ValueError if the provider is unknown, disabled, or missing a key.
    """
    all_prov = prov_mod.load_providers()
    cfg = all_prov.get(provider_id)
    if not cfg:
        raise ValueError(f"unknown provider: {provider_id}")
    if not cfg.get("enabled"):
        raise ValueError(f"provider {provider_id} is disabled (enable it in Settings)")

    proto = cfg.get("protocol")
    model = model_name or prov_mod.model_for_provider(all_prov, provider_id)
    temperature, model_kwargs = _sampling(cfg)
    base_url = cfg.get("base_url") or None

    if proto == "anthropic":
        key = cfg.get("api_key")
        if not key:
            raise ValueError("Anthropic API Key 为空 — 在 设置 → 模型 API 填写")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=model or "claude-3-7-sonnet-20250219",
            api_key=key,
            base_url=base_url,
            temperature=temperature if temperature is not None else 0.7,
            model_kwargs=model_kwargs or {"max_tokens": 4096},
            streaming=True,
        )

    # openai / openai-compatible
    key = cfg.get("api_key") or ""
    if proto == "openai" and not base_url:
        base_url = "https://api.openai.com/v1"
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model or "gpt-4o",
        api_key=key or "not-needed",
        base_url=base_url,
        temperature=temperature if temperature is not None else 0.7,
        model_kwargs=model_kwargs or {"max_tokens": 8192},
        streaming=True,
    )
