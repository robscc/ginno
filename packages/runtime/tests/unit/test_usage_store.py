"""Unit tests for the persistent usage log (usage-stats-design.md §4)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from ginno_runtime import paths, usage_store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_store_cache():
    usage_store.reset_cache()
    yield
    usage_store.reset_cache()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _record(ts_offset_days: float = 0, **kw) -> None:
    defaults = dict(
        input_tokens=1000,
        output_tokens=100,
        cache_read_tokens=600,
        cache_creation_tokens=0,
        provider="anthropic",
        model="claude-x",
        session_id="s1",
        project_slug="default",
    )
    defaults.update(kw)
    ts = time.time() - ts_offset_days * 86400
    usage_store.record(ts=ts, **defaults)


def test_record_appends_jsonl_line(isolated_home):
    _record()
    _record(output_tokens=200)
    p = paths.usage_dir() / f"requests-{_today()}.jsonl"
    lines = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert lines[0]["input_tokens"] == 1000
    assert lines[0]["source"] == "chat"
    assert lines[1]["output_tokens"] == 200
    # privacy: no prompt/response content fields
    assert not any(k in lines[0] for k in ("prompt", "content", "messages"))


def test_record_never_raises_on_bad_input(isolated_home):
    # non-serializable junk in optional fields must not break a turn
    usage_store.record(
        input_tokens=1, output_tokens=1, provider="p", model="m",
        turn_id=None, latency_ms=None,
    )
    assert usage_store.load_day(_today())


def test_overview_aggregates_window(isolated_home):
    _record(input_tokens=1000, output_tokens=100, cache_read_tokens=600)
    _record(input_tokens=500, output_tokens=50, cache_read_tokens=0,
            provider="custom", model="deepseek-chat", session_id="s2")
    ov = usage_store.aggregate_overview(30)
    assert ov["totals"]["input_tokens"] == 1500
    assert ov["totals"]["output_tokens"] == 150
    assert ov["totals"]["calls"] == 2
    assert ov["totals"]["cache_hit_ratio"] == round(600 / 1500, 4)
    assert ov["today"]["calls"] == 2
    assert len(ov["daily"]) == 30
    assert ov["daily"][-1]["date"] == _today()
    assert ov["sessions_active"] == 2
    # providers + models sorted by tokens desc
    assert ov["providers"][0]["provider"] == "anthropic"
    assert ov["providers"][1]["provider"] == "custom"
    assert ov["models"][0]["model"] == "claude-x"
    assert ov["models"][1]["model"] == "deepseek-chat"


def test_overview_window_clamps_to_data(isolated_home):
    _record(ts_offset_days=10)
    ov7 = usage_store.aggregate_overview(7)
    ov30 = usage_store.aggregate_overview(30)
    assert ov7["totals"]["calls"] == 0          # outside the 7-day window
    assert ov30["totals"]["calls"] == 1         # inside the 30-day window


def test_hourly_buckets_by_local_hour(isolated_home):
    _record()
    _record()
    h = usage_store.aggregate_hourly()
    assert h["date"] == _today()
    assert len(h["hours"]) == 24
    now_hour = time.localtime().tm_hour
    assert h["hours"][now_hour]["calls"] == 2
    assert sum(b["calls"] for b in h["hours"]) == 2


def test_sessions_aggregate_and_sort(isolated_home):
    _record(input_tokens=1000, session_id="s1")
    _record(input_tokens=3000, session_id="s2", provider="custom", model="m2")
    rows = usage_store.aggregate_sessions(None, None)
    assert [r["session_id"] for r in rows] == ["s2", "s1"]  # sorted by total desc
    assert rows[1]["input_tokens"] == 1000
    assert rows[1]["calls"] == 1
    by_input = usage_store.aggregate_sessions(None, None, sort="input")
    assert by_input[0]["session_id"] == "s2"


def test_session_totals_and_empty(isolated_home):
    _record(input_tokens=1000, cache_read_tokens=700, session_id="s1")
    st = usage_store.session_totals("s1")
    assert st["input_tokens"] == 1000
    assert st["cache_hit_ratio"] == 0.7
    assert usage_store.session_totals("missing") is None


def test_requests_filter_and_paginate(isolated_home):
    for i in range(7):
        _record(session_id=f"s{i % 2}", provider="anthropic" if i % 2 else "openai")
    all_rows = usage_store.query_requests(page_size=50)
    assert all_rows["total"] == 7
    page1 = usage_store.query_requests(page=1, page_size=3)
    page2 = usage_store.query_requests(page=2, page_size=3)
    assert len(page1["rows"]) == 3 and len(page2["rows"]) == 3
    # newest first
    ts = [r["ts"] for r in page1["rows"]]
    assert ts == sorted(ts, reverse=True)
    anthro = usage_store.query_requests(provider="anthropic")
    assert anthro["total"] == 3
    s0 = usage_store.query_requests(session_id="s0")
    assert s0["total"] == 4
    assert usage_store.query_requests(source="goal")["total"] == 0


def test_records_landed_on_their_own_day(isolated_home):
    _record(ts_offset_days=3, input_tokens=777)
    past = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    assert usage_store.load_day(past)[0]["input_tokens"] == 777
    assert all(e["input_tokens"] != 777 for e in usage_store.load_day(_today()))


def test_cleanup_removes_old_files_only(isolated_home):
    udir = paths.usage_dir()
    udir.mkdir(parents=True, exist_ok=True)
    old_day = (datetime.now() - timedelta(days=91)).strftime("%Y-%m-%d")
    keep_day = (datetime.now() - timedelta(days=89)).strftime("%Y-%m-%d")
    (udir / f"requests-{old_day}.jsonl").write_text("{}\n")
    (udir / f"requests-{keep_day}.jsonl").write_text("{}\n")
    (udir / f"requests-{_today()}.jsonl").write_text("{}\n")
    removed = usage_store.cleanup(90)
    assert removed == 1
    assert not (udir / f"requests-{old_day}.jsonl").exists()
    assert (udir / f"requests-{keep_day}.jsonl").exists()
    assert (udir / f"requests-{_today()}.jsonl").exists()


def test_corrupt_lines_skipped(isolated_home):
    _record()
    p = paths.usage_dir() / f"requests-{_today()}.jsonl"
    with open(p, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
    assert len(usage_store.load_day(_today())) == 1
