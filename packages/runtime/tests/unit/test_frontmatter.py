"""Unit tests for markdown frontmatter parsing + body helpers."""

from __future__ import annotations

import pytest

from ginno_runtime.knowledge import frontmatter as fm

pytestmark = pytest.mark.unit


def test_split_frontmatter_valid():
    raw = "---\ntitle: Hello\ntags: [a, b]\n---\n# Body\ncontent"
    meta, body = fm.split_frontmatter(raw)
    assert meta["title"] == "Hello"
    assert meta["tags"] == ["a", "b"]
    assert body.startswith("# Body")


def test_split_frontmatter_absent():
    raw = "# Just a doc\nno frontmatter"
    meta, body = fm.split_frontmatter(raw)
    assert meta == {}
    assert body == raw


def test_split_frontmatter_invalid_yaml():
    raw = "---\n: : : not yaml [\n---\nbody"
    meta, body = fm.split_frontmatter(raw)
    assert meta == {}
    assert "body" in body


def test_split_frontmatter_nested_permission():
    raw = "---\npermission:\n  level: restricted\n  readers: [user:bob]\n---\nx"
    meta, _ = fm.split_frontmatter(raw)
    assert meta["permission"]["level"] == "restricted"
    assert meta["permission"]["readers"] == ["user:bob"]


def test_extract_title_h1():
    assert fm.extract_title("intro\n# My Title\nbody") == "My Title"
    assert fm.extract_title("no heading here") is None
    # H2 is not a title
    assert fm.extract_title("## subheading") is None


def test_extract_summary_first_paragraph():
    body = "# Heading\n\nFirst paragraph line one\nline two.\n\nSecond paragraph."
    assert fm.extract_summary(body) == "First paragraph line one line two."


def test_extract_summary_skips_code_table_quote():
    body = "```\ncode here\n```\n| a | b |\n> quote\n\nReal summary text."
    assert fm.extract_summary(body) == "Real summary text."


def test_extract_summary_truncates():
    body = "x" * 500
    out = fm.extract_summary(body, max_chars=200)
    assert len(out) <= 201  # 200 chars + ellipsis
    assert out.endswith("…")


def test_extract_wikilinks_variants():
    body = "See [[Alpha]] and [[Beta|the beta]] plus [[Gamma#section]] and [[Alpha]] again."
    assert fm.extract_wikilinks(body) == ["Alpha", "Beta", "Gamma"]


def test_as_list_forms():
    assert fm._as_list(["a", "b"]) == ["a", "b"]
    assert fm._as_list("a, b ,c") == ["a", "b", "c"]
    assert fm._as_list(None) == []
    assert fm._as_list("solo") == ["solo"]
