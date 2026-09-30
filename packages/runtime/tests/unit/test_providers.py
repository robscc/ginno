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


# ---- validate_configs: the `default_model ∈ models` gate ---------------------
# This gate runs on every PUT of the config list, so a rejection here makes the
# whole settings form unsaveable — the user sees an error and has no way forward.


def _cfg(**over):
    """A minimal config that passes normalization."""
    base = {
        "id": "c1",
        "name": "C",
        "protocol": "openai-compatible",
        "base_url": "https://example.invalid/v1",
        "api_key": "k",
        "models": [],
    }
    base.update(over)
    return base


def test_validate_repairs_a_case_only_default_model_mismatch():
    """A case-only mismatch must be REPAIRED, not rejected.

    Model ids are lowercase on several gateways while a hand-written or migrated
    config may use mixed case (``GLM-5.3-Flash`` vs ``glm-5.3-flash``). Raising
    made the provider's default permanently unsaveable from the UI, so the value
    is normalized to the spelling that is actually in the list.
    """
    out = providers.validate_configs(
        [_cfg(models=["glm-5.3-flash", "glm-5.3"], default_model="GLM-5.3-Flash")]
    )
    assert out[0]["default_model"] == "glm-5.3-flash"


def test_validate_still_rejects_a_default_model_that_is_not_listed():
    """The repair covers case only. A genuinely absent model is a real
    misconfiguration (it would fail at the gateway), so it stays an error."""
    with pytest.raises(ValueError, match="不在 models 内"):
        providers.validate_configs([_cfg(models=["glm-5.3"], default_model="gpt-4o")])


def test_validate_accepts_an_unlisted_default_when_there_is_no_list():
    """A legacy config stores only `model` (no `models[]`), so the gate's
    `cfg["models"]` guard must let it through — otherwise every legacy provider
    would be unsaveable."""
    out = providers.validate_configs([_cfg(models=[], default_model="GLM-5.3-Flash")])
    assert out[0]["default_model"] == "GLM-5.3-Flash"
