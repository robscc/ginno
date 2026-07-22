"""Model factory — build a LangChain chat model from a provider id.

Reads provider config from settings.json `providers` (see providers.py).
Sampling params: temperature as a top-level kwarg; max_tokens via
model_kwargs (robust across langchain versions). base_url + api_key as
top-level kwargs (verified present on ChatAnthropic + ChatOpenAI).
"""

from __future__ import annotations

import os
from typing import Any

from . import providers as prov_mod


def _sampling(cfg: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    temperature = cfg.get("temperature")
    model_kwargs: dict[str, Any] = {}
    mt = cfg.get("max_tokens")
    if mt:
        model_kwargs["max_tokens"] = int(mt)
    return (float(temperature) if temperature is not None else None, model_kwargs)


def build_model(provider_id: str, model_name: str | None = None, enable_search: bool | None = None):
    """Return a LangChain chat model for the given provider id.

    `model_name` overrides the provider's configured default/model.
    Raises ValueError if the provider is unknown, disabled, or missing a key.

    Test seam: when ``GINNO_FAKE_LLM`` is set, return a deterministic
    ScriptedChatModel (script from ``GINNO_FAKE_LLM_SCRIPTS``) instead of a real
    provider. Off by default; has zero effect on the normal production path.
    """
    if os.environ.get("GINNO_FAKE_LLM"):
        import logging

        logging.getLogger(__name__).warning(
            "GINNO_FAKE_LLM is set — using a deterministic ScriptedChatModel "
            "instead of a real provider"
        )
        from .testing.fake_model import build_fake_model

        return build_fake_model()

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

        chat_kwargs: dict[str, Any] = dict(
            model=model or "claude-3-7-sonnet-20250219",
            api_key=key,
            base_url=base_url,
            temperature=temperature if temperature is not None else 0.7,
            model_kwargs=model_kwargs or {"max_tokens": 4096},
            streaming=True,
        )
        # Some Anthropic-compatible gateways (corporate model hubs / proxies) expect
        # the token in `Authorization: Bearer ...` instead of `x-api-key`. The official
        # Anthropic API uses x-api-key, so this is opt-in via `bearer_auth`.
        if cfg.get("bearer_auth"):
            chat_kwargs["default_headers"] = {"Authorization": f"Bearer {key}"}
        return ChatAnthropic(**chat_kwargs)

    # openai / openai-compatible
    key = cfg.get("api_key") or ""
    if proto == "openai" and not base_url:
        base_url = "https://api.openai.com/v1"
    from langchain_openai import ChatOpenAI

    chat_kw: dict[str, Any] = dict(
        model=model or "gpt-4o",
        api_key=key or "not-needed",
        base_url=base_url,
        temperature=temperature if temperature is not None else 0.7,
        model_kwargs=model_kwargs or {"max_tokens": 8192},
        streaming=True,
    )
    # `enable_search` (None = follow the provider config) lets OpenAI-compatible
    # gateways such as Qwen / DashScope compatible-mode run the model's built-in
    # web search. langchain-openai forwards `extra_body` verbatim into the
    # request body, so the agent can search the web on its own when it needs to.
    es = cfg.get("enable_search") if enable_search is None else enable_search
    if es:
        chat_kw["extra_body"] = {"enable_search": True}
    return ChatOpenAI(**chat_kw)
