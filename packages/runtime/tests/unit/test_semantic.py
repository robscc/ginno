"""Semantic retrieval: degradation + lexical/semantic fusion.

These tests never touch the network or require the `rag` extra: they exercise
the "off / unavailable" path of ``get_semantic_index`` and the retriever's
fusion logic via a fake semantic index, so a missing sentence-transformers /
lancedb install can't break them (and a present one can't trigger a model
download).
"""

from __future__ import annotations

import pytest

from ginno_runtime.knowledge import semantic as sem_mod
from ginno_runtime.knowledge.retriever import WikiRetriever
from ginno_runtime.knowledge.types import KnowledgeConfig, WikiEntry

pytestmark = pytest.mark.unit


def _entry(rel: str, title: str, summary: str = "") -> WikiEntry:
    return WikiEntry(path=f"/v/{rel}", relative_path=rel, title=title, summary=summary, checksum="c" + rel)


class FakeSemantic:
    def __init__(self, scores: dict[str, float], ready: bool = True) -> None:
        self.ready = ready
        self._scores = scores

    def scores(self, query: str) -> dict[str, float]:
        return self._scores


def _cfg(**kw) -> KnowledgeConfig:
    base = {"enabled": True, "vault_path": "/v"}
    base.update(kw)
    return KnowledgeConfig(**base)


def test_semantic_off_returns_none_without_touching_model():
    # use_semantic=False must short-circuit before any embedding work.
    assert sem_mod.get_semantic_index(_cfg(use_semantic=False), [_entry("a.md", "A")]) is None


def test_semantic_index_scores_empty_until_ready():
    si = sem_mod.SemanticIndex(_cfg(), embed_model="")
    assert si.ready is False
    assert si.scores("anything") == {}


def test_reset_semantic_clears_cache():
    sem_mod.reset_semantic()  # must not raise even when empty
    assert sem_mod.get_semantic_index(_cfg(use_semantic=False), []) is None


def test_retriever_fusion_lifts_semantic_hit():
    entries = [
        _entry("alpha.md", "Alpha unrelated prose", "nothing about the query here at all"),
        _entry("beta.md", "Beta", "the exact lexical keyword matches this page"),
    ]
    # "beta" wins on lex; "alpha" has zero lexical signal but a strong semantic one.
    fake = FakeSemantic({"alpha.md": 0.95, "beta.md": 0.1})

    lexical = WikiRetriever(entries).retrieve("keyword", top_k=5, min_score=0.0)
    fused = WikiRetriever(entries).retrieve(
        "keyword", top_k=5, min_score=0.0, semantic=fake, semantic_weight=1.0
    )

    lex_top = lexical[0].entry.relative_path
    fused_top = fused[0].entry.relative_path
    # lexical-only prefers the keyword page…
    assert lex_top == "beta.md"
    # …but fusion promotes the semantic hit to the top.
    assert fused_top == "alpha.md"
    # the semantic contribution is surfaced in matched_terms
    assert any(t.startswith("semantic:") for t in fused[0].matched_terms)


def test_retriever_ignores_not_ready_semantic():
    entries = [_entry("a.md", "the keyword lives here", "keyword keyword")]
    not_ready = FakeSemantic({"a.md": 0.9}, ready=False)
    res = WikiRetriever(entries).retrieve("keyword", top_k=5, min_score=0.0, semantic=not_ready)
    assert res and not any(t.startswith("semantic:") for t in res[0].matched_terms)


def test_retriever_semantic_only_when_no_lexical_tokens():
    # punctuation-only query yields no lexical tokens; a ready semantic index can
    # still surface a hit instead of the empty early-return.
    entries = [_entry("a.md", "Alpha", "some summary")]
    fake = FakeSemantic({"a.md": 0.8})
    res = WikiRetriever(entries).retrieve("!!!", top_k=5, min_score=0.0, semantic=fake, semantic_weight=1.0)
    assert len(res) == 1 and res[0].entry.relative_path == "a.md"
