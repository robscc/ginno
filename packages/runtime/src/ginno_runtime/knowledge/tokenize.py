"""Tokenization for wiki retrieval (and later association).

Chinese text is split into character unigrams/bigrams/trigrams so substring
matching works without a segmenter; latin text is split on word boundaries and
lowercased. This mirrors Molly's ``tokenizeQuery`` and needs no external deps.
"""

from __future__ import annotations

import re

# CJK Unified Ideographs (basic block) — enough for simplified/traditional Chinese.
_CJK_RE = re.compile(r"[一-龥]")
_CJK_RUN_RE = re.compile(r"[一-龥]+")
# Keep letters (any script), digits, and hyphen; everything else becomes a split point.
_NON_WORD_RE = re.compile(r"[^\w-]", re.UNICODE)


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def _cjk_ngrams(run: str) -> list[str]:
    """Unigrams + bigrams + trigrams of a contiguous CJK run."""
    grams: list[str] = []
    n = len(run)
    for size in (1, 2, 3):
        if n >= size:
            grams.extend(run[i : i + size] for i in range(n - size + 1))
    return grams


def _latin_tokens(segment: str) -> list[str]:
    out: list[str] = []
    for tok in _NON_WORD_RE.sub(" ", segment).split():
        tok = tok.strip("_-")
        if len(tok) >= 2:
            out.append(tok.lower())
    return out


def tokenize_query(text: str) -> list[str]:
    """Tokenize a query into a de-duplicated list of match tokens.

    For each whitespace-delimited segment:
      * if it contains CJK, emit CJK unigram/bigram/trigram (and any latin
        sub-tokens, so mixed segments like "使用LangGraph" keep both);
      * otherwise emit lowercased latin tokens of length >= 2.
    """
    if not text:
        return []
    tokens: set[str] = set()
    for segment in text.split():
        if has_cjk(segment):
            for run in _CJK_RUN_RE.findall(segment):
                tokens.update(_cjk_ngrams(run))
            # also keep latin tokens inside mixed segments
            latin = _latin_tokens(_CJK_RUN_RE.sub(" ", segment))
            tokens.update(latin)
        else:
            tokens.update(_latin_tokens(segment))
    return list(tokens)
