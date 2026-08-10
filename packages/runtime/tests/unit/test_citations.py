"""Unit tests for the unified citation framework (docs/citations-design.md)."""

from __future__ import annotations

import pytest

from ginno_runtime.knowledge import citations as cit

pytestmark = pytest.mark.unit


# ----------------------------- parsing ----------------------------- #
def test_parse_basic_block():
    text = (
        "结论如下。\n\n"
        "<ginno_citations>\n"
        "wiki|Ginno/Wiki/concepts/x.md|note=[机制依据]\n"
        "web|s2|note=[计费规则]\n"
        "</ginno_citations>"
    )
    entries = cit.parse_citation_block(text)
    assert [e["kind"] for e in entries] == ["wiki", "web"]
    assert entries[0]["ref"] == "Ginno/Wiki/concepts/x.md"
    assert entries[0]["note"] == "机制依据"
    assert entries[1]["ref"] == "s2"


def test_parse_note_without_brackets_keeps_web_ref_clean():
    """The contract is ``note=[…]`` but models often emit ``note=…`` (no
    brackets). The note must still split off so a web ref stays a clean,
    openable URL — otherwise it's left as ``<url>|note=…`` and won't open."""
    text = (
        "<ginno_citations>\n"
        "web|https://www.news.cn/a/c.html|note=新华网报道深圳发布政策\n"
        "wiki|Ginno/Wiki/x.md|note=[带括号的仍可用]\n"
        "</ginno_citations>"
    )
    entries = cit.parse_citation_block(text)
    assert len(entries) == 2
    web = entries[0]
    assert web["kind"] == "web"
    assert web["ref"] == "https://www.news.cn/a/c.html"  # no "|note=…" glued on
    assert web["note"] == "新华网报道深圳发布政策"
    assert entries[1]["ref"] == "Ginno/Wiki/x.md"
    assert entries[1]["note"] == "带括号的仍可用"


def test_parse_tolerates_whitespace_missing_note_and_dupes():
    text = (
        "<ginno_citations>\n"
        "  wiki|A.md  \n"
        "\n"
        "wiki|a.md|note=[重复应去重]\n"
        "unknown|zzz|note=[未知 kind 忽略]\n"
        "web|https://x.com/a/|note=[]\n"
        "</ginno_citations>"
    )
    entries = cit.parse_citation_block(text)
    assert len(entries) == 2
    assert entries[0] == {"kind": "wiki", "ref": "A.md", "note": ""}
    assert entries[1]["kind"] == "web"


def test_parse_legacy_wiki_block():
    text = "<ginno_wiki_citations>\nGinno/Wiki/y.md|note=[旧格式]\n</ginno_wiki_citations>"
    entries = cit.parse_citation_block(text)
    assert entries == [{"kind": "wiki", "ref": "Ginno/Wiki/y.md", "note": "旧格式"}]


def test_parse_no_block_returns_empty_and_cap_applies():
    assert cit.parse_citation_block("没有引用的普通回答") == []
    lines = "\n".join(f"wiki|p{i}.md" for i in range(cit.MAX_CITATIONS_PER_TURN + 5))
    entries = cit.parse_citation_block(f"<ginno_citations>\n{lines}\n</ginno_citations>")
    assert len(entries) == cit.MAX_CITATIONS_PER_TURN


def test_strip_citation_block():
    text = "正文。\n<ginno_citations>\nwiki|A.md\n</ginno_citations>"
    assert cit.strip_citation_block(text) == "正文。"


# --------------------------- normalization --------------------------- #
def test_normalize_web_ref():
    a = cit.normalize_web_ref("https://WWW.Example.com/a/?utm_source=x&k=1#frag")
    b = cit.normalize_web_ref("http://example.com/a?k=1")
    assert a == b == "https://example.com/a?k=1"


# ---------------------------- validation ---------------------------- #
def _sources():
    return [
        {"id": "s1", "kind": "wiki", "identity": "Ginno/Wiki/concepts/x.md", "title": "概念X",
         "origin": "injected", "depth": "injected"},
        {"id": "s2", "kind": "web", "identity": "https://docs.example.com/page", "title": "Docs",
         "origin": "search", "depth": "snippet"},
        {"id": "s3", "kind": "web", "identity": "https://deep.example.com/full", "title": "Full",
         "origin": "fetch", "depth": "fetched"},
    ]


def test_validate_verified_by_path_title_and_id():
    entries = cit.parse_citation_block(
        "<ginno_citations>\n"
        "wiki|ginno/wiki/concepts/x|note=[路径宽容]\n"
        "wiki|概念X|note=[标题也可]\n"
        "web|s2|note=[编号]\n"
        "web|https://deep.example.com/full/|note=[URL 规范化]\n"
        "</ginno_citations>"
    )
    out = cit.validate_citations(entries, _sources())
    assert [o["status"] for o in out] == ["verified", "verified", "verified", "verified"]
    assert out[0]["identity"] == "Ginno/Wiki/concepts/x.md"
    assert out[3]["depth"] == "fetched"


def test_validate_index_only_and_unverified():
    entries = cit.parse_citation_block(
        "<ginno_citations>\n"
        "wiki|Ginno/Wiki/not-injected.md|note=[在索引不在本轮]\n"
        "wiki|不存在的页.md|note=[幻觉]\n"
        "web|https://never-seen.com/|note=[编造 URL]\n"
        "</ginno_citations>"
    )
    out = cit.validate_citations(
        entries,
        _sources(),
        resolve_wiki=lambda ref: "Ginno/Wiki/not-injected.md" if "not-injected" in ref else None,
    )
    assert [o["status"] for o in out] == ["index_only", "unverified", "unverified"]
    assert out[0]["identity"] == "Ginno/Wiki/not-injected.md"


# ------------------------- turn source registry ------------------------- #
def test_registry_begin_register_end(isolated_home):
    lst = cit.begin_turn_sources("sess-1")
    token = cit.CURRENT_TURN_SOURCES.set(lst)
    try:
        src = cit.register_source({"kind": "web", "identity": "https://a.b/", "title": "A"})
        assert src["id"] == "s1"
        cit.register_source({"kind": "web", "identity": "https://c.d/", "title": "C"})
    finally:
        cit.CURRENT_TURN_SOURCES.reset(token)
    assert [s["id"] for s in cit.peek_turn_sources("sess-1")] == ["s1", "s2"]
    ended = cit.end_turn_sources("sess-1")
    assert len(ended) == 2
    assert cit.end_turn_sources("sess-1") == []  # idempotent pop


def test_register_outside_turn_is_noop():
    assert cit.register_source({"kind": "web", "identity": "https://x/"}) is None


def test_begin_resets_on_retry(isolated_home):
    cit.begin_turn_sources("s")
    token = cit.CURRENT_TURN_SOURCES.set(cit.peek_turn_sources("s"))
    try:
        cit.register_source({"kind": "web", "identity": "https://old/"})
    finally:
        cit.CURRENT_TURN_SOURCES.reset(token)
    fresh = cit.begin_turn_sources("s")  # same turn_id retry
    assert fresh == []


def test_strip_unclosed_block():
    """A truncated block (max_tokens / watchdog kill) must not leak raw lines."""
    text = "正文。\n<ginno_citations>\nwiki|A.md|note=[用途]\nweb|s2|no"
    assert cit.strip_citation_block(text) == "正文。"
    # closed block still handled
    assert cit.strip_citation_block("x<ginno_citations>\nw|a\n</ginno_citations>y") == "xy"


def test_norm_wiki_ref_no_charset_collision():
    """`.env` must NOT normalize to `env` (lstrip('./') char-set bug)."""
    assert cit._norm_wiki_ref(".env") == ".env"
    assert cit._norm_wiki_ref("./Ginno/Wiki/X.md") == "ginno/wiki/x"
    assert cit._norm_wiki_ref("Ginno/Wiki/X.md") == "ginno/wiki/x"
