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


# --- provider output-content filter (2026-10-08 turn 4b3a219e) -------------
# anthropic raised APIStatusError(code="InvalidParameter",
# "Output data may contain inappropriate content.") MID-STREAM while
# summarizing ten scraped news pages. The raw APIStatusError surfaced on the
# error card with no hint, and retrying it just gets blocked again.


class _FakeAPIStatusError(Exception):
    """Stands in for anthropic.APIStatusError without importing the SDK."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(
            f"{{'request_id': 'abc', 'code': 'InvalidParameter', 'message': {message!r}}}"
        )


def test_content_filter_error_gets_specific_key():
    fields = turn_error_fields(
        _FakeAPIStatusError("Output data may contain inappropriate content.")
    )
    assert fields["i18n_key"] == "stream.turn_failed.content_filter"
    assert "Retrying the same request will be blocked" in fields["message"]
    assert "error" in fields["params"]


def test_content_filter_detected_through_cause_chain():
    """Wrappers (RetryError, task groups) nest the real error — the detector
    must walk __cause__/__context__ like _is_transient_model_error does."""
    inner = _FakeAPIStatusError("Output data may contain inappropriate content.")
    outer = RuntimeError("model call failed")
    outer.__cause__ = inner
    assert turn_error_fields(outer)["i18n_key"] == "stream.turn_failed.content_filter"


def test_content_filter_is_not_auto_retried():
    """Same content is rejected again on retry, so it must stay OUT of the
    transient auto-retry path (400 is not 408/429/5xx)."""
    from ginno_runtime.api.stream.engine import _is_content_filter_error, _is_transient_model_error

    e = _FakeAPIStatusError("Output data may contain inappropriate content.")
    assert _is_content_filter_error(e)
    assert not _is_transient_model_error(e)


def test_unrelated_api_error_stays_generic():
    """Only the content-filter message maps to the new key — an ordinary 400
    (e.g. a malformed request) keeps the generic card."""
    fields = turn_error_fields(_FakeAPIStatusError("invalid model field"))
    assert fields["i18n_key"] == "stream.turn_failed"


# --- recursion pre-warn threshold -------------------------------------------
def test_recursion_warn_threshold_scales_with_limit():
    """The steer must leave the model room to wrap up. A flat 10-step margin
    warns only 10 steps from a raised 400-step wall; the proportional margin
    keeps ~10% of headroom regardless of how high the limit is configured."""
    from ginno_runtime.api.stream.engine import recursion_warn_threshold

    # default 128: margin 12 → 2*58 == 116 >= 116 trips at agent step 58
    assert recursion_warn_threshold(128) == 58
    # raised 400: margin 40 → trips at 180 (2*180 == 360 == 400-40)
    assert recursion_warn_threshold(400) == 180
    # always leaves at least the 10-step floor
    assert recursion_warn_threshold(50) >= (50 - 10) // 2 - 1
    # degenerate limit must not produce a threshold that can never fire
    assert recursion_warn_threshold(0) == 0


# --- user-steer budget continuation ----------------------------------------
# A turn the USER steered mid-flight can legitimately outrun the budget it was
# sized for. Those turns resume from the checkpoint with a fresh budget (up to
# STEER_CONT_MAX); the SYSTEM recursion-warn steer must NOT — it exists to
# break runaway loops, and extra budget would let one run longer.


def test_steer_id_prefix_separates_system_from_user():
    from ginno_runtime.api.stream.engine import STEER_ID_SYSTEM_PREFIX

    assert STEER_ID_SYSTEM_PREFIX == "recursion-warn-"


def test_steer_continuation_is_bounded():
    from ginno_runtime.api.stream.engine import STEER_CONT_MAX

    # must be > 0 (a steered turn needs at least one continuation) and small
    # (an unbounded refresh lets a runaway loop live forever)
    assert 0 < STEER_CONT_MAX <= 3


def test_system_steer_does_not_count_as_user_work():
    """The recursion-warn steer must not make a turn eligible for continuation
    — otherwise the anti-loop mechanism would extend the loop it exists to stop."""
    from ginno_runtime.api.stream.engine import (
        STEER_CONT_MAX,
        STEER_ID_SYSTEM_PREFIX,
        _is_user_steer,
    )

    assert not _is_user_steer({"steer_id": f"{STEER_ID_SYSTEM_PREFIX}turn-1"})
    assert _is_user_steer({"steer_id": "m4"})
    assert _is_user_steer({"steer_id": "user-2026-10-08-abc"})
    assert _is_user_steer({})  # no id → treat as user (conservative)
    assert STEER_CONT_MAX >= 1
