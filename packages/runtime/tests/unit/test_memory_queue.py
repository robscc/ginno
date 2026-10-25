"""Draft queue lifecycle: TTL expiry, capacity eviction, runs log, usage.

Regression cover for the 2026-10-01 stall (docs/dream-mode-design.md §1.2): one
unreviewed draft used to block every later distillation for 24 days while the
pool grew to 290 entries, and none of it was visible anywhere.
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime import usage_store
from ginno_runtime.memory import summarize as S
from ginno_runtime.memory.pool import append_to_pool, pool_count
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit


def _patch_model(monkeypatch, text: str, usage: dict | None = None) -> None:
    fake = ScriptedChatModel(scripts=[script(text=text, usage=usage)])
    monkeypatch.setattr("ginno_runtime.memory.summarize.build_model", lambda *a, **k: fake)


def _backdate(draft_id: str, days: float) -> None:
    """Age a pending draft (TTL is measured from meta.created_at)."""
    meta = S._load_meta(draft_id)
    assert meta is not None
    meta["created_at"] = time.time() - days * 86400
    S._save_meta(draft_id, meta)


async def _make_draft(monkeypatch, text="## 技术栈\n- TypeScript") -> dict:
    append_to_pool("s1", "dev", "偏好 TypeScript")
    _patch_model(monkeypatch, text)
    return await S.create_draft()


@pytest.mark.asyncio
async def test_success_records_run_and_usage(isolated_home, monkeypatch):
    append_to_pool("s1", "dev", "偏好 TypeScript")
    _patch_model(
        monkeypatch,
        "## 技术栈\n- TypeScript",
        usage={"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
    )

    result = await S.create_draft()
    assert result["ok"] is True and result["draft"]

    runs = S.run_history()
    assert runs and runs[0]["status"] == "success"
    assert runs[0]["input_tokens"] == 1200
    assert runs[0]["output_tokens"] == 300
    assert runs[0]["draft_id"] == result["id"]

    today = time.strftime("%Y-%m-%d", time.localtime())
    rows = [e for e in usage_store.load_day(today) if e.get("source") == "memory"]
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 1200
    assert rows[0]["turn_id"] == runs[0]["run_id"]


@pytest.mark.asyncio
async def test_ttl_expiry_unblocks_the_pipeline(isolated_home, monkeypatch):
    first = await _make_draft(monkeypatch)
    assert S.has_draft()

    # Nothing expires before the TTL.
    assert S.expire_stale_drafts(ttl_days=7) == []
    assert S.has_draft()

    _backdate(first["id"], days=8)
    expired = S.expire_stale_drafts(ttl_days=7)
    assert expired == [first["id"]]
    assert not S.has_draft()
    assert S.list_drafts(status="expired")[0]["id"] == first["id"]

    # The expiry is in the run log, not silent.
    skipped = [r for r in S.run_history() if r["status"] == "skipped"]
    assert skipped and skipped[0]["reason"] == "expired_ttl"

    # …and a new run is possible again.
    second = await _make_draft(monkeypatch, "## 技术栈\n- TypeScript\n- pnpm")
    assert second["ok"] is True and second["id"] != first["id"]


@pytest.mark.asyncio
async def test_queue_capacity_evicts_oldest(isolated_home, monkeypatch):
    # Two pending drafts can only arise from older bugs/races — build them by
    # hand so the cap logic itself is covered.
    ids = []
    for i in range(2):
        did = f"{int(time.time()) + i}-test"
        S._atomic_write(S._draft_paths(did)[0], f"## A{i}\n- x")
        S._save_meta(
            did,
            {
                "id": did,
                "created_at": time.time() + i,
                "status": S.PENDING,
                "chars": 10,
                "budget": 3000,
                "pool_entries": 1,
            },
        )
        ids.append(did)

    expired = S.expire_stale_drafts(ttl_days=0, max_pending=1)
    assert expired == [ids[0]]  # oldest one goes
    assert [d["id"] for d in S.list_drafts(status="pending")] == [ids[1]]


@pytest.mark.asyncio
async def test_legacy_single_slot_is_migrated(isolated_home):
    paths.memory_draft_path().parent.mkdir(parents=True, exist_ok=True)
    paths.memory_draft_path().write_text("## 旧草稿\n- 内容", encoding="utf-8")
    paths.memory_draft_meta_path().write_text(
        '{"created_at": 1790823827.5, "trigger": "auto", "pool_entries": 30,'
        ' "pool_cutoff": 1790823798.2, "budget": 3000,'
        ' "previous": "", "previous_sha1": ""}',
        encoding="utf-8",
    )

    did = S.migrate_legacy_draft()
    assert did
    assert not paths.memory_draft_path().exists()
    payload = S.draft_payload(did)
    assert payload["draft"] == "## 旧草稿\n- 内容"
    assert payload["trigger"] == "auto"
    assert payload["status"] == "pending"
    # An old draft that sat past the TTL expires on the next sweep.
    _backdate(did, days=30)
    assert S.expire_stale_drafts(ttl_days=7) == [did]


@pytest.mark.asyncio
async def test_apply_refuses_over_budget_then_truncates(isolated_home):
    did = f"{int(time.time())}-budget"
    body = "## A\n- " + "x" * 100
    S._atomic_write(S._draft_paths(did)[0], body)
    S._save_meta(
        did,
        {
            "id": did,
            "created_at": time.time(),
            "status": S.PENDING,
            "chars": len(body),
            "budget": 20,
            "pool_entries": 1,
            "pool_cutoff": time.time(),
            "previous": "",
            "previous_sha1": "",
        },
    )

    refused = S.apply_draft(draft_id=did)
    assert refused["ok"] is False and refused["error"] == "over_budget"
    assert S.has_draft()

    ok = S.apply_draft(draft_id=did, truncate=True)
    assert ok["ok"] is True
    written = paths.memory_index_path().read_text(encoding="utf-8")
    assert len(written) <= 20
    assert S.list_drafts()[0]["status"] == S.APPLIED


@pytest.mark.asyncio
async def test_apply_keeps_history_and_clears_pool(isolated_home, monkeypatch):
    result = await _make_draft(monkeypatch)
    applied = S.apply_draft()
    assert applied["ok"] is True
    assert pool_count() == 0
    assert not S.has_draft()

    rows = S.list_drafts()
    assert rows[0]["id"] == result["id"]
    assert rows[0]["status"] == S.APPLIED
    assert rows[0]["resolved_at"] is not None


def test_truncate_to_budget_cuts_on_line_boundaries():
    text = "## A\n- one\n- two\n- three"
    out = S.truncate_to_budget(text, 12)
    assert out == "## A\n- one"
    assert S.truncate_to_budget("short", 100) == "short"


def _write_pending(draft_id: str, body: str, budget: int = 3000) -> None:
    S._atomic_write(S._draft_paths(draft_id)[0], body)
    S._save_meta(
        draft_id,
        {
            "id": draft_id,
            "created_at": time.time(),
            "status": S.PENDING,
            "chars": len(body),
            "budget": budget,
            "pool_entries": 3,
            "pool_cutoff": time.time(),
            "previous": "",
            "previous_sha1": "",
        },
    )


def test_split_draft_keeps_selected_sections(isolated_home):
    did = f"{int(time.time())}-split"
    body = "## A\n- a1\n- a2\n\n## B\n- b1\n\n## C\n- c1"
    _write_pending(did, body)

    out = S.split_draft(did, ["## A", "C"])  # with and without the prefix
    assert out["ok"] is True
    assert out["id"] != did
    assert "a1" in out["draft"] and "c1" in out["draft"]
    assert "b1" not in out["draft"]
    assert out["status"] == "pending"

    statuses = {d["id"]: d["status"] for d in S.list_drafts()}
    assert statuses[did] == S.DISCARDED
    assert statuses[out["id"]] == S.PENDING

    # Nothing selected → refused, original untouched.
    assert S.split_draft(out["id"], ["## 不存在"])["error"] == "empty_selection"


def test_snapshot_replaces_same_day_and_trend_reads(isolated_home):
    from ginno_runtime.memory.pool import append_to_pool
    from ginno_runtime.memory.status import memory_trend, record_snapshot

    append_to_pool("s", "dev", "x")
    row = record_snapshot()
    assert row and row["pool"] == 1
    record_snapshot()  # same day replaces the line instead of appending
    trend = memory_trend(30)
    assert len(trend) == 1
    assert trend[0]["pool"] == 1
    assert "memory_chars" in trend[0] and "budget" in trend[0]


def test_run_history_orders_by_timestamp(isolated_home):
    p = paths.memory_runs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"ts": 100, "run_id": "old"},
                {"ts": 300, "run_id": "new"},
                {"ts": 200, "run_id": "mid"},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert [r["run_id"] for r in S.run_history()] == ["new", "mid", "old"]
