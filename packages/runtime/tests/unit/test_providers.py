"""Unit tests for the provider registry: defaults merge, selection, verify (no network)."""

from __future__ import annotations

import pytest

from ginno_runtime import paths, providers

pytestmark = pytest.mark.unit


def test_load_providers_merges_defaults(isolated_home):
    provs = providers.load_providers(settings={})
    assert set(providers.PROVIDER_IDS) <= set(provs)
    # every known provider carries its full default record
    assert provs["anthropic"]["protocol"] == "anthropic"
    assert provs["openai"]["base_url"] == "https://api.openai.com/v1"
    assert provs["anthropic"]["enabled"] is False


def test_load_providers_merges_stored_over_defaults(isolated_home):
    provs = providers.load_providers(
        settings={"providers": {"custom": {"enabled": True, "api_key": "k"}}}
    )
    assert provs["custom"]["enabled"] is True
    assert provs["custom"]["api_key"] == "k"
    # untouched defaults still present
    assert provs["custom"]["protocol"] == "openai-compatible"


def test_default_provider_fallthrough_to_custom(isolated_home):
    # nothing enabled -> returns configured default_provider or "custom"
    assert providers.get_default_provider(settings={}) == "custom"


def test_default_provider_prefers_enabled(isolated_home):
    settings = {
        "default_provider": "custom",
        "providers": {"anthropic": {"enabled": True, "api_key": "x"}},
    }
    # the configured default (custom) is not enabled, so first enabled wins
    assert providers.get_default_provider(settings=settings) == "anthropic"


def test_default_provider_honors_enabled_choice(isolated_home):
    settings = {
        "default_provider": "openai",
        "providers": {"openai": {"enabled": True, "api_key": "x"}},
    }
    assert providers.get_default_provider(settings=settings) == "openai"


def test_save_providers_normalizes_over_defaults(isolated_home):
    paths.ensure_layout()
    saved = providers.save_providers({"custom": {"enabled": True, "api_key": "abc"}})
    # partial write still yields a full record
    assert saved["custom"]["protocol"] == "openai-compatible"
    assert saved["custom"]["enabled"] is True
    # persisted to disk
    reloaded = providers.load_providers()
    assert reloaded["custom"]["api_key"] == "abc"


def test_model_for_provider(isolated_home):
    provs = providers.load_providers(settings={})
    assert providers.model_for_provider(provs, "anthropic") == "claude-3-7-sonnet-20250219"


def test_verify_unknown_provider_no_network(isolated_home):
    paths.ensure_layout()
    result = providers.verify("does-not-exist")
    assert result["ok"] is False
    assert "unknown provider" in result["error"]


def test_verify_anthropic_missing_key_no_network(isolated_home):
    paths.ensure_layout()
    # default anthropic provider has an empty key -> early return, no client call
    result = providers.verify("anthropic")
    assert result["ok"] is False
    assert "API Key" in result["error"]
