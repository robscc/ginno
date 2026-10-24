"""Suggestion heuristics: context pressure.

The rule used to compare *lifetime* session tokens against one context window
(a long session reported 9063%) instead of the current prompt.
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import providers, suggestions, usage_store

pytestmark = pytest.mark.unit


def _meta(sid: str = "s1", provider: str = "custom") -> dict:
    return {"id": sid, "provider": provider, "project_slug": "default"}


@pytest.fixture
def window(monkeypatch):
    def _set(size: int) -> None:
        monkeypatch.setattr(
            providers,
            "load_configs",
            lambda settings=None: [{"id": "custom", "context_window": size}],
        )

    return _set


def _rec(sid: str, tokens: int, *, ts: float) -> None:
    usage_store.record(
        input_tokens=tokens,
        output_tokens=0,
        provider="custom",
        model="m",
        session_id=sid,
        ts=ts,
    )


def test_context_ignores_lifetime_totals(isolated_home, window):
    """40 calls x 50k = 2M lifetime against a 100k window must not warn."""
    window(100_000)
    now = time.time()
    for i in range(40):
        _rec("s1", 50_000, ts=now - 500 + i)
    _rec("s1", 5_000, ts=now)  # current context is small (post-compaction)

    assert suggestions._context_suggestion(_meta()) is None


def test_context_uses_the_newest_prompt(isolated_home, window):
    window(100_000)
    now = time.time()
    _rec("s1", 95_000, ts=now - 60)
    _rec("s1", 10_000, ts=now)

    assert suggestions._context_suggestion(_meta()) is None


def test_context_warns_on_the_current_prompt(isolated_home, window):
    window(100_000)
    _rec("s1", 80_000, ts=time.time())

    hint = suggestions._context_suggestion(_meta())
    assert hint is not None and hint["id"] == "context.pressure"
    assert "80k" in hint["detail"] and "80%" in hint["detail"]
    assert "累计" not in hint["detail"]
    assert hint["action"]["type"] == "compact"
