"""Unit tests for build_model, including the opt-in GINNO_FAKE_LLM seam."""

from __future__ import annotations

import pytest

from ginno_runtime import models, paths
from ginno_runtime.testing.fake_model import ScriptedChatModel

pytestmark = pytest.mark.unit


def test_fake_llm_seam_returns_scripted_model(monkeypatch):
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    model = models.build_model("custom")
    assert isinstance(model, ScriptedChatModel)


def test_fake_llm_seam_reads_scripts_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    scripts_file = tmp_path / "turns.json"
    scripts_file.write_text(
        '[{"content": "hello"}, '
        '{"content": "", "tool_calls": [{"name": "read_file", "args": {"path": "x"}}]}]'
    )
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts_file))
    model = models.build_model("custom")
    assert len(model.scripts) == 2
    assert model.scripts[1].tool_calls[0]["name"] == "read_file"


def test_seam_off_by_default_uses_real_path(isolated_home, monkeypatch):
    # With the seam off, the normal provider validation applies: a disabled
    # provider raises ValueError (no network is touched).
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)
    paths.ensure_layout()
    with pytest.raises(ValueError):
        models.build_model("custom")


# ---- anthropic User-Agent identity -----------------------------------------
# langchain-anthropic stamps every request with its own
# ``User-Agent: langchain-anthropic/<ver>``, which makes external gateways
# (magpie, cc-switch usage views) attribute Ginno's traffic to
# "langchain-anthropic". build_model must override it with a ginno UA.


def _anthropic_cfg(**extra):
    cfg = {
        "enabled": True,
        "protocol": "anthropic",
        "api_key": "sk-test",
        "base_url": "http://127.0.0.1:3425",
        "model": "test-model",
        "max_tokens": 100,
        "temperature": 0.5,
        "timeout_s": 30,
    }
    cfg.update(extra)
    return cfg


def test_anthropic_ua_is_ginno(isolated_home, monkeypatch):
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)
    cfg = _anthropic_cfg()
    monkeypatch.setattr(models.prov_mod, "load_providers", lambda: {"p": cfg})
    m = models.build_model("p")
    assert m.default_headers["User-Agent"].startswith("ginno/")


def test_anthropic_bearer_auth_keeps_ginno_ua(isolated_home, monkeypatch):
    # bearer_auth's Authorization must ride in the same default_headers dict
    # as the UA — not replace it (that regression would re-label Ginno's
    # traffic as langchain-anthropic on Bearer-style gateways).
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)
    cfg = _anthropic_cfg(bearer_auth=True)
    monkeypatch.setattr(models.prov_mod, "load_providers", lambda: {"p": cfg})
    m = models.build_model("p")
    assert m.default_headers["User-Agent"].startswith("ginno/")
    assert m.default_headers["Authorization"] == "Bearer sk-test"
