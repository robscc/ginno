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
