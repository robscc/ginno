"""Unit tests for the wiki tokenizer (Chinese n-grams + latin tokens)."""

from __future__ import annotations

import pytest

from ginno_runtime.knowledge.tokenize import has_cjk, tokenize_query

pytestmark = pytest.mark.unit


def test_empty():
    assert tokenize_query("") == []
    assert tokenize_query("   ") == []


def test_latin_tokens_lowercased():
    assert sorted(tokenize_query("Hello World")) == ["hello", "world"]


def test_short_latin_tokens_dropped():
    # single-char latin tokens are dropped (length < 2); order is not guaranteed
    assert sorted(tokenize_query("I a bb cc")) == ["bb", "cc"]


def test_chinese_unigram_bigram_trigram():
    toks = set(tokenize_query("权限"))
    assert {"权", "限", "权限"} <= toks


def test_chinese_trigrams():
    toks = set(tokenize_query("权限节点"))
    # unigrams + bigrams + trigrams
    assert {"权", "限", "节", "点"} <= toks            # unigrams
    assert {"权限", "限节", "节点"} <= toks            # bigrams
    assert {"权限节", "限节点"} <= toks                # trigrams


def test_mixed_segment_keeps_both():
    toks = set(tokenize_query("使用LangGraph"))
    assert "使用" in toks            # CJK bigram
    assert "langgraph" in toks       # latin token preserved


def test_dedupe():
    toks = tokenize_query("权限 权限 权限")
    assert toks.count("权限") == 1


def test_has_cjk():
    assert has_cjk("权限") is True
    assert has_cjk("permission") is False
    assert has_cjk("mix权限") is True
