"""format_turn_error — error-card text for turn failures.

Known failures get a Ginno-specific remedy appended to the raw exception dump
(LangGraph's GraphRecursionError advice points at langchain docs, which is not
actionable inside Ginno — the fix lives in ~/.ginno/settings.json).
"""

from __future__ import annotations

import json

import pytest
from langgraph.errors import GraphRecursionError

from ginno_runtime import paths
from ginno_runtime.api.stream.engine import format_turn_error


def test_generic_exception_stays_raw():
    e = ValueError("boom")
    assert format_turn_error(e) == "ValueError: boom"


def test_recursion_error_gets_settings_hint(monkeypatch, isolated_home):
    monkeypatch.delenv("GINNO_RECURSION_LIMIT", raising=False)
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"runtime": {"recursion_limit": 128}}))

    e = GraphRecursionError(
        "Recursion limit of 128 reached without hitting a stop condition."
    )
    text = format_turn_error(e)

    # First line keeps the raw dump — substring gates (subagent_scheduler's
    # recursion wrap) keep matching "GraphRecursionError".
    assert text.startswith("GraphRecursionError: Recursion limit of 128")
    assert "recursion_limit=128" in text
    assert "~/.ginno/settings.json" in text
    assert '"runtime"' in text and '"recursion_limit"' in text
    # The stale langchain docs link must not be echoed to the user.
    assert "langchain" not in text


def test_recursion_error_reports_configured_limit(monkeypatch, isolated_home):
    monkeypatch.delenv("GINNO_RECURSION_LIMIT", raising=False)
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"runtime": {"recursion_limit": 256}}))

    text = format_turn_error(GraphRecursionError("Recursion limit of 256 reached."))
    assert "recursion_limit=256" in text


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), RuntimeError("x")])
def test_non_graph_errors_untouched(exc):
    assert format_turn_error(exc) == f"{type(exc).__name__}: {exc}"
