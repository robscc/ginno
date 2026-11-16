"""Suggestion heuristics: context pressure.

The rule used to compare *lifetime* session tokens against one context window
(a long session reported 9063%) instead of the current prompt. Since
2026-11-16 the threshold binds to the AUTO-COMPACTION trigger point
(min(compact_threshold_tokens, window×0.75)) at 0.85× — not the window's
fixed 70% — so big-window models (1M: threshold 500k = only 50% of the
window) warn BEFORE compaction fires instead of never (the old inversion).
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import providers, suggestions, usage, usage_store

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
    """80k prompt, 100k window → threshold 75k, 80k ≥ 0.85×75k → warn."""
    window(100_000)
    _rec("s1", 80_000, ts=time.time())

    hint = suggestions._context_suggestion(_meta())
    assert hint is not None and hint["id"] == "context.pressure"
    assert "80k" in hint["detail"] and "107%" in hint["detail"]
    assert "累计" not in hint["detail"]
    assert hint["action"]["type"] == "compact"


def test_context_below_threshold_is_quiet(isolated_home, window):
    """60k / 100k window（= 阈值 75k 的 80%，< 0.85 分档）不预警。"""
    window(100_000)
    _rec("s1", 60_000, ts=time.time())

    assert suggestions._context_suggestion(_meta()) is None


def test_context_big_window_warns_before_compaction(isolated_home, window):
    """1M 窗口倒挂回归（2026-11-16）：旧口径 70%×窗口 = 700k 才预警，而
    自动压缩在 500k 就触发——预警永远迟到。新口径 0.85×500k = 425k 起
    预警：600k prompt（窗口占比仅 60%，旧逻辑静默）必须出卡。"""
    window(1_000_000)
    _rec("s1", 600_000, ts=time.time())

    hint = suggestions._context_suggestion(_meta())
    assert hint is not None and hint["id"] == "context.pressure"
    assert "500k" in hint["detail"]


def test_context_state_carries_threshold(isolated_home, window):
    """usage.context_state 与压缩触发点同式（钉住双写一致性）。"""
    from ginno_runtime.compaction import _effective_compact_threshold
    from ginno_runtime.world_state import context_settings

    window(1_000_000)
    st = usage.context_state(400_000, "custom", "m")
    assert st["window"] == 1_000_000
    assert st["threshold"] == 500_000  # min(500k default, 1M×0.75)
    assert st["compact_pct"] == 80.0
    # 与 compaction._effective_compact_threshold 同式（session 伪 dict 提供
    # provider/model，与 st 的解析链一致）。
    eff = _effective_compact_threshold(
        context_settings(), {"model_provider": "custom", "model_name": "m"}
    )
    assert st["threshold"] == eff

    window(200_000)
    st2 = usage.context_state(100_000, "custom", "m")
    assert st2["threshold"] == 150_000  # 200k×0.75 < 500k
    assert st2["compact_pct"] == pytest.approx(66.7, abs=0.1)
