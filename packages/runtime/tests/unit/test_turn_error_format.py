"""turn_error_fields / format_turn_error — error-card payload for turn failures.

i18n contract (i18n-design.md §3): known langgraph failures get a specific
``stream.turn_failed.<suffix>`` key + params (UI renders the localized
remedy; a stale bundle falls back to the English ``message``, which carries
the same hint). Control-flow signals keep the generic key with no hint —
they are not failures, and a "what to do next" tip would mislead.
"""

from __future__ import annotations

import json

import pytest
from langgraph.errors import (
    EmptyChannelError,
    EmptyInputError,
    GraphInterrupt,
    GraphRecursionError,
    InvalidUpdateError,
    NodeCancelledError,
    NodeTimeoutError,
    ParentCommand,
)

from ginno_runtime import paths
from ginno_runtime.api.stream.engine import format_turn_error, turn_error_fields


def test_generic_exception_uses_generic_key():
    fields = turn_error_fields(ValueError("boom"))
    assert fields == {
        "message": "ValueError: boom",
        "i18n_key": "stream.turn_failed",
        "params": {"error": "ValueError: boom"},
    }


def test_recursion_error_fields(monkeypatch, isolated_home):
    monkeypatch.delenv("GINNO_RECURSION_LIMIT", raising=False)
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"runtime": {"recursion_limit": 128}}))

    e = GraphRecursionError(
        "Recursion limit of 128 reached without hitting a stop condition."
    )
    fields = turn_error_fields(e)

    # Message: raw dump first line (substring gates like subagent_scheduler's
    # recursion wrap keep matching), then the English remedy fallback.
    assert fields["message"].startswith("GraphRecursionError: Recursion limit of 128")
    assert "~/.ginno/settings.json" in fields["message"]
    # The stale langchain docs link must not be echoed to the user.
    assert "langchain" not in fields["message"]
    # i18n: specific key, params carry the live configured limit.
    assert fields["i18n_key"] == "stream.turn_failed.recursion_limit"
    assert fields["params"] == {"error": fields["message"].splitlines()[0], "limit": 128}


def test_recursion_error_reports_configured_limit(monkeypatch, isolated_home):
    monkeypatch.delenv("GINNO_RECURSION_LIMIT", raising=False)
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"runtime": {"recursion_limit": 256}}))

    fields = turn_error_fields(GraphRecursionError("Recursion limit of 256 reached."))
    assert fields["params"]["limit"] == 256


def test_empty_input_key():
    fields = turn_error_fields(EmptyInputError("Falsify nothing"))
    assert fields["i18n_key"] == "stream.turn_failed.empty_input"
    assert fields["params"]["error"].startswith("EmptyInputError:")
    assert "re-runs the original input" in fields["message"]


@pytest.mark.parametrize(
    "exc,suffix",
    [
        (InvalidUpdateError("bad channel update"), "state"),
        (EmptyChannelError(), "state"),
        (NodeTimeoutError("node_x", 180.0, kind="run", run_timeout=120.0), "node_timeout"),
    ],
)
def test_state_and_timeout_keys(exc, suffix):
    fields = turn_error_fields(exc)
    assert fields["i18n_key"] == f"stream.turn_failed.{suffix}"
    assert fields["message"].startswith(f"{type(exc).__name__}:")
    assert "sidecar.log" in fields["message"]


@pytest.mark.parametrize(
    "exc",
    [
        GraphInterrupt([]),
        ParentCommand(None),
        NodeCancelledError("node_x"),
        KeyboardInterrupt(),
        RuntimeError("x"),
    ],
)
def test_control_flow_and_unknown_errors_use_generic_key(exc):
    """Interrupts / cancels / non-langgraph errors: generic key, no hint —
    they are not failures, so a "what to do next" tip would mislead."""
    fields = turn_error_fields(exc)
    assert fields["i18n_key"] == "stream.turn_failed"
    assert fields["message"] == f"{type(exc).__name__}: {exc}"


def test_format_turn_error_is_message_field():
    e = GraphRecursionError("Recursion limit of 128 reached.")
    assert format_turn_error(e) == turn_error_fields(e)["message"]
    assert format_turn_error(ValueError("x")) == "ValueError: x"
