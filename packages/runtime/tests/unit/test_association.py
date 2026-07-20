"""Unit tests for the association engine (signals, skip rules, clusters, discover)."""

from __future__ import annotations

import time

import pytest

from ginno_runtime.knowledge.association import AssociationEngine
from ginno_runtime.knowledge.types import WikiEntry

pytestmark = pytest.mark.unit

NOW = time.time()


def _e(title, summary="", tags=None, links=None, sources=None, modified=NOW, rel=None):
    return WikiEntry(
        path=f"/v/{rel or title}.md",
        relative_path=f"{rel or title}.md",
        title=title,
        summary=summary,
        tags=tags or [],
        links=links or [],
        modified=modified,
        checksum="x",
        sources=sources or [],
    )


def _engine(entries, backlinks=None, orphans=None):
    bl = backlinks or (lambda _t: [])
    return AssociationEngine(entries, backlinks=bl, orphans=orphans or set())


def _edge(eng, t1, t2):
    for e in eng.edges:
        if {e.a, e.b} == {t1, t2}:
            return e
    return None


# ---------------- signals ---------------- #
def test_tag_overlap_drives_edge():
    a = _e("权限节点", "deny ask allow", tags=["arch", "permission"])
    b = _e("权限策略", "deny ask allow 匹配", tags=["arch", "permission"])
    eng = _engine([a, b])
    e = _edge(eng, "权限节点", "权限策略")
    assert e is not None
    assert e.signals["tag_overlap"] == pytest.approx(1.0)
    assert e.score >= 0.3


def test_unrelated_pages_no_edge():
    a = _e("权限节点", "deny ask allow interrupt", tags=["arch", "permission"])
    c = _e("红烧肉", "五花肉 冰糖 酱油 炖煮", tags=["cooking"])
    eng = _engine([a, c])
    assert _edge(eng, "权限节点", "红烧肉") is None


def test_co_occur_signal_via_backlinks():
    x = _e("X", "alpha unique text one", tags=["x"])
    y = _e("Y", "beta unique text two", tags=["y"])
    # Z links to both X and Y → both have backlink {Z}
    eng = _engine([x, y], backlinks=lambda t: ["Z"] if t in ("x", "y") else [])
    sig = eng._signals(x, y)
    assert sig["co_occur"] == pytest.approx(1.0)
    # co_occur(0.20) + temporal(same mtime ->0.10) reaches the 0.3 edge threshold
    assert _edge(eng, "X", "Y") is not None


def test_temporal_far_apart_is_zero():
    a = _e("A", "shared token shared token", tags=["t"], modified=NOW)
    b = _e("B", "shared token shared token", tags=["t"], modified=NOW - 30 * 86400)
    eng = _engine([a, b])
    assert eng._signals(a, b)["temporal"] == 0.0


def test_hierarchy_signal():
    parent = _e(
        "架构总览",
        "本系统包含 权限节点 与 checkpointer 等模块，整体采用动态图。",
        tags=["arch", "permission", "graph", "overview", "design"],
    )
    child = _e("权限节点", "deny ask allow", tags=["arch", "permission"])
    eng = _engine([parent, child])
    assert eng._signals(parent, child)["hierarchy"] == 1.0
    assert _edge(eng, "架构总览", "权限节点") is not None


# ---------------- skip rules ---------------- #
def test_skip_shared_sources():
    a = _e("概念A", "foo bar baz", tags=["t"], sources=["raw/one.md"])
    b = _e("概念B", "foo bar baz", tags=["t"], sources=["raw/one.md"])
    eng = _engine([a, b])
    assert eng.edges == []


def test_skip_explicit_link():
    a = _e("A", "foo bar", tags=["t"], links=["B"])
    b = _e("B", "foo bar", tags=["t"])
    eng = _engine([a, b])
    assert eng.edges == []


def test_skip_near_duplicate_titles():
    a = _e("权限节点", "deny ask allow", tags=["t"])
    b = _e("权限节点设计", "deny ask allow", tags=["t"])
    eng = _engine([a, b])
    assert eng.edges == []


# ---------------- clusters / discover / related ---------------- #
def test_cluster_from_triangle():
    # three mutually tag-identical pages form a dense cluster
    a = _e("A", "graph node edge", tags=["arch", "graph"])
    b = _e("B", "graph node edge", tags=["arch", "graph"])
    c = _e("C", "graph node edge", tags=["arch", "graph"])
    eng = _engine([a, b, c])
    clusters = eng.clusters()
    assert len(clusters) == 1
    assert set(clusters[0]["members"]) == {"A", "B", "C"}
    assert clusters[0]["density"] >= 0.4


def test_find_related_returns_neighbors_and_clusters():
    a = _e("A", "graph node edge", tags=["arch", "graph"])
    b = _e("B", "graph node edge", tags=["arch", "graph"])
    c = _e("C", "graph node edge", tags=["arch", "graph"])
    z = _e("Z", "cooking recipe food", tags=["cooking"])
    eng = _engine([a, b, c, z])
    r = eng.find_related("A")
    titles = {x["title"] for x in r["related"]}
    assert titles == {"B", "C"}
    assert len(r["clusters"]) == 1


def test_discover_strong_isolated_merge():
    a = _e("A", "graph node edge routing", tags=["arch", "graph"])
    b = _e("B", "graph node edge routing", tags=["arch", "graph"])
    lone = _e("Lone", "zzz qqq www", tags=["other"])
    # a co-citing page pushes the A-B composite (semantic+tag+co_occur+temporal) over STRONG
    eng = _engine([a, b, lone], backlinks=lambda t: ["Z"] if t in ("a", "b") else [])
    d = eng.discover()
    assert d["stats"]["pages"] == 3
    ab = next(p for p in d["strong"] if {p["a"], p["b"]} == {"A", "B"})
    assert ab["score"] >= 0.8
    assert d["isolated"] == ["Lone"]
    # identical tags + text → semantic & tag high → merge candidate
    assert any({p["a"], p["b"]} == {"A", "B"} for p in d["merge_candidates"])
