"""Unit tests for the wiki citation usage ledger (citations-design.md §6.1)."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.knowledge import usage

pytestmark = pytest.mark.unit


def test_record_injected_and_cited(isolated_home):
    usage.record_injected(["A.md", "B.md", "A.md"], {"A.md": "ck-a", "B.md": "ck-b"})
    usage.record_cited("A.md", session_id="s1", turn_id="t1")
    usage.record_cited("B.md", index_only=True)
    data = json.loads(usage.usage_path().read_text())
    assert data["A.md"]["injected"] == 1  # deduped per call (= per turn)
    assert data["A.md"]["cited"] == 1
    assert data["A.md"]["checksum"] == "ck-a"
    assert data["A.md"]["last_session"] == "s1"
    assert data["B.md"]["cited_index_only"] == 1
    assert "cited" not in data["B.md"]  # index_only never counts as cited


def test_record_invalid_ring(isolated_home):
    usage.record_invalid([f"bad{i}.md" for i in range(25)])
    data = json.loads(usage.usage_path().read_text())
    inv = data["_invalid"]
    assert inv["cited"] == 25
    assert len(inv["samples"]) == 20  # capped ring


def test_top_sort_and_rate(isolated_home):
    for _ in range(3):
        usage.record_injected(["hot.md"])
    usage.record_cited("hot.md")
    usage.record_injected(["cold.md"] * 5)
    rows = usage.top("rate", 10)
    by_path = {r["path"]: r for r in rows}
    assert by_path["hot.md"]["rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert by_path["cold.md"]["rate"] == 0.0
    assert rows[0]["path"] == "hot.md"


def test_summary_and_reset(isolated_home):
    usage.record_injected(["A.md"])
    usage.record_cited("A.md")
    usage.record_invalid(["ghost.md"])
    s = usage.summary()
    assert s["total_injected"] == 1
    assert s["total_cited"] == 1
    assert s["citation_rate"] == 1.0
    assert s["invalid_cited"] == 1
    assert s["top_cited"][0]["path"] == "A.md"
    usage.reset()
    assert usage.all_usage() == {}


def test_is_drifted():
    assert usage.is_drifted({"checksum": "a"}, "b") is True
    assert usage.is_drifted({"checksum": "a"}, "a") is False
    assert usage.is_drifted({}, "a") is False  # no baseline -> unknown


def test_corrupt_ledger_reads_empty(isolated_home):
    usage.usage_path().parent.mkdir(parents=True, exist_ok=True)
    usage.usage_path().write_text("{not json")
    assert usage.all_usage() == {}
