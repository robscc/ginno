"""Unit tests for the multi-signal retriever."""

from __future__ import annotations

import time

import pytest

from ginno_runtime.knowledge.retriever import WikiRetriever, score_entry
from ginno_runtime.knowledge.tokenize import tokenize_query
from ginno_runtime.knowledge.types import WikiEntry

pytestmark = pytest.mark.unit


def _entry(title="T", summary="", tags=None, links=None, modified=None, rel="x.md"):
    return WikiEntry(
        path=f"/v/{rel}",
        relative_path=rel,
        title=title,
        summary=summary,
        tags=tags or [],
        links=links or [],
        modified=modified if modified is not None else time.time(),
    )


def test_tag_weights_highest():
    # modified=0 disables the recency bump so we can isolate the tag weight
    e = _entry(title="unrelated", summary="unrelated", tags=["kubernetes"], modified=0)
    s, matched = score_entry(e, tokenize_query("kubernetes"))
    assert s == pytest.approx(0.4)
    assert any(m.startswith("tag:") for m in matched)


def test_title_and_summary_weights():
    e = _entry(title="kubernetes guide", summary="all about kubernetes", tags=[])
    s, _ = score_entry(e, tokenize_query("kubernetes"))
    # title 0.3 + summary 0.15 (+ small recency bump)
    assert s >= 0.45


def test_score_capped_at_one():
    e = _entry(title="权限 节点 设计", summary="权限 节点 设计 详解", tags=["权限", "节点"])
    s, _ = score_entry(e, tokenize_query("权限节点设计"))
    assert s <= 1.0


def test_recency_bump_applies_only_when_matched():
    now = time.time()
    recent = _entry(title="kubernetes", modified=now)
    old = _entry(title="kubernetes", modified=now - 30 * 86400)
    s_recent, _ = score_entry(recent, tokenize_query("kubernetes"))
    s_old, _ = score_entry(old, tokenize_query("kubernetes"))
    assert s_recent > s_old


def test_no_match_zero_score():
    e = _entry(title="cooking", summary="recipes", tags=["food"])
    s, matched = score_entry(e, tokenize_query("kubernetes"))
    assert s == 0.0
    assert matched == []


def test_retrieve_filters_and_sorts():
    entries = [
        _entry(title="kubernetes guide", tags=["kubernetes"], rel="k.md"),
        _entry(title="cooking", tags=["food"], rel="c.md"),
        _entry(title="advanced kubernetes", tags=["kubernetes"], rel="a.md"),
    ]
    results = WikiRetriever(entries).retrieve("kubernetes", top_k=5, min_score=0.3)
    titles = [r.entry.title for r in results]
    assert "cooking" not in titles
    assert set(titles) == {"kubernetes guide", "advanced kubernetes"}
    assert results[0].score >= results[-1].score


def test_retrieve_top_k_limits():
    entries = [_entry(title=f"kubernetes {i}", rel=f"{i}.md") for i in range(10)]
    results = WikiRetriever(entries).retrieve("kubernetes", top_k=3, min_score=0.3)
    assert len(results) == 3


def test_retrieve_empty_query():
    assert WikiRetriever([_entry(title="x")]).retrieve("") == []


def test_wikilink_boost():
    # a high scorer links to a low scorer; the low scorer gets boosted
    strong = _entry(title="kubernetes", links=["Cheat Sheet"], rel="s.md")
    weak = _entry(title="Cheat Sheet", summary="quick ref", rel="w.md")  # no token match
    results = WikiRetriever([strong, weak]).retrieve("kubernetes", top_k=5, min_score=0.05)
    weak_r = next(r for r in results if r.entry.title == "Cheat Sheet")
    assert "wikilink" in weak_r.matched_terms
    assert weak_r.score > 0


def test_search_by_tag_and_title():
    entries = [_entry(title="A", tags=["x"], rel="a.md"), _entry(title="B", tags=["y"], rel="b.md")]
    ret = WikiRetriever(entries)
    assert [r.entry.title for r in ret.search_by_tag("x")] == ["A"]
    assert [r.entry.title for r in ret.search_by_title("B")] == ["B"]


def test_result_snippet_capped():
    e = _entry(title="kubernetes", summary="z" * 500)
    r = WikiRetriever([e]).retrieve("kubernetes")[0]
    assert len(r.snippet) == 300
