"""Nightly distillation window (docs/dream-mode-design.md §4.4).

The window is built into the scheduler tick rather than a user schedule: it is
driven by the knowledge config, never appears in schedules.json, and must be
idempotent per local day.
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime import scheduler as sch
from ginno_runtime.memory import summarize as S
from ginno_runtime.memory.pool import append_to_pool

pytestmark = pytest.mark.unit


def _write_config(**kw) -> None:
    cfg = {
        "enabled": True,
        "capture": True,
        "auto_summarize": True,
        "pool_flush_threshold": 1,
        "dream_nightly": True,
        "dream_at": "00:00",  # the window is always open in tests
        **kw,
    }
    paths.settings_path().write_text(json.dumps({"knowledge": cfg}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    monkeypatch.setattr(sch, "_DREAM_CHECKED_AT", 0.0)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(sch.shared, "_push_global_event", _noop)


@pytest.mark.asyncio
async def test_nightly_dream_creates_one_draft_per_day(isolated_home, monkeypatch):
    _write_config()
    append_to_pool("s1", "dev", "内容")

    calls: list[str] = []

    async def fake_create(trigger="manual", provider=None):
        calls.append(trigger)
        return {"ok": True, "id": "d1", "draft": "## A\n- x", "draft_exists": False, "pool_entries": 1}

    monkeypatch.setattr("ginno_runtime.memory.create_draft", fake_create)

    await sch._maybe_nightly_dream()
    assert calls == ["schedule"]
    state = json.loads((paths.home() / "memory" / "dream-state.json").read_text(encoding="utf-8"))
    assert state["last_run"] == time.strftime("%Y-%m-%d")

    # Same day again: the throttle is irrelevant (forced open), the day guard holds.
    monkeypatch.setattr(sch, "_DREAM_CHECKED_AT", 0.0)
    await sch._maybe_nightly_dream()
    assert calls == ["schedule"]


@pytest.mark.asyncio
async def test_nightly_dream_respects_switches_and_pending(isolated_home, monkeypatch):
    calls: list[str] = []

    async def fake_create(trigger="manual", provider=None):
        calls.append(trigger)
        return {"ok": True, "id": "d1", "draft": "x", "draft_exists": False}

    monkeypatch.setattr("ginno_runtime.memory.create_draft", fake_create)

    # Switch off → nothing happens.
    _write_config(dream_nightly=False)
    append_to_pool("s1", "dev", "内容")
    monkeypatch.setattr(sch, "_DREAM_CHECKED_AT", 0.0)
    await sch._maybe_nightly_dream()
    assert calls == []

    # Pool below the threshold → nothing happens.
    _write_config(pool_flush_threshold=99)
    monkeypatch.setattr(sch, "_DREAM_CHECKED_AT", 0.0)
    await sch._maybe_nightly_dream()
    assert calls == []

    # A draft is already pending → review first, don't stack another.
    _write_config(pool_flush_threshold=1)
    did = f"{int(time.time())}-p"
    S._atomic_write(S._draft_paths(did)[0], "## A\n- x")
    S._save_meta(did, {"id": did, "created_at": time.time(), "status": S.PENDING, "chars": 8})
    monkeypatch.setattr(sch, "_DREAM_CHECKED_AT", 0.0)
    await sch._maybe_nightly_dream()
    assert calls == []
