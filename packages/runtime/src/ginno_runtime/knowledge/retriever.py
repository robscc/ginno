"""Multi-signal wiki retrieval (no embeddings).

Scoring per token (each field counts once per token):
    tag  +0.4   (bidirectional substring between token and tag)
    title +0.3  (substring)
    summary +0.15 (substring)
Plus a recency bump (<= 7 days) and a wikilink-graph boost for pages linked
from high-scoring hits. Mirrors Molly's WikiRetriever.
"""

from __future__ import annotations

import time
from typing import Any

from .tokenize import tokenize_query
from .types import RetrievalResult, WikiEntry

W_TAG = 0.4
W_TITLE = 0.3
W_SUMMARY = 0.15
RECENCY_MAX = 0.05
RECENCY_WINDOW_DAYS = 7
WIKILINK_BOOST = 0.1
BOOST_TRIGGER_SCORE = 0.3


def score_entry(entry: WikiEntry, tokens: list[str]) -> tuple[float, list[str]]:
    """Return (score capped at 1.0, matched_terms like 'tag:x'/'title:y').

    Each FIELD contributes its weight at most once per query. The old per-token
    sum let a CJK query — which tokenizes into many overlapping uni/bi/trigrams —
    saturate every page that shared a single character to ~100%, collapsing the
    ranking (the UI showed long lists of tied "100%" hits).
    """
    title = entry.title.lower()
    summary = entry.summary.lower()
    tags = [t.lower() for t in entry.tags if t]
    toks = [t.lower() for t in tokens if t]
    score = 0.0
    matched: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        if term not in seen:
            seen.add(term)
            matched.append(term)

    tag_hits = [t for t in tags if any(tok in t or t in tok for tok in toks)]
    if tag_hits:
        score += W_TAG
        for t in tag_hits:
            _add(f"tag:{t}")
    title_hits = [tok for tok in toks if tok in title]
    if title_hits:
        score += W_TITLE
        for tok in title_hits:
            _add(f"title:{tok}")
    summary_hits = [tok for tok in toks if tok in summary]
    if summary_hits:
        score += W_SUMMARY
        for tok in summary_hits:
            _add(f"summary:{tok}")

    if score > 0 and entry.modified:
        days = (time.time() - entry.modified) / 86400.0
        if 0 <= days < RECENCY_WINDOW_DAYS:
            score += RECENCY_MAX * (1 - days / RECENCY_WINDOW_DAYS)
    return min(score, 1.0), matched


class WikiRetriever:
    def __init__(self, entries: list[WikiEntry]) -> None:
        self.entries = entries

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        semantic: Any = None,
        semantic_weight: float = 0.5,
    ) -> list[RetrievalResult]:
        tokens = tokenize_query(query)
        # cosine similarity per page from the (optional) semantic index; degrades
        # to {} when semantic is None / not ready / errors, i.e. lexical-only.
        sem_scores: dict[str, float] = {}
        if semantic is not None and getattr(semantic, "ready", False):
            try:
                sem_scores = semantic.scores(query) or {}
            except Exception:  # noqa: BLE001
                sem_scores = {}

        if (not tokens and not sem_scores) or not self.entries:
            return []

        scored = [score_entry(e, tokens) + (e,) for e in self.entries]  # (score, matched, entry)

        # collect link targets surfaced by strong lexical hits
        linked: set[str] = set()
        for s, _matched, e in scored:
            if s >= BOOST_TRIGGER_SCORE:
                linked.update(link.lower() for link in e.links)

        results: list[RetrievalResult] = []
        for s, matched, e in scored:
            if s < BOOST_TRIGGER_SCORE and e.title.lower() in linked:
                s = min(s + WIKILINK_BOOST, 1.0)
                matched = [*matched, "wikilink"]
            ss = sem_scores.get(e.relative_path, 0.0)
            combined = min(s + (semantic_weight * ss if ss else 0.0), 1.0)
            if ss >= 0.3:
                matched = [*matched, f"semantic:{round(ss, 2)}"]
            if combined >= min_score:
                results.append(
                    RetrievalResult(
                        entry=e,
                        score=combined,
                        matched_terms=matched,
                        snippet=(e.summary or "")[:300],
                    )
                )
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def search_by_tag(self, tag: str) -> list[RetrievalResult]:
        t = tag.lower()
        out = [
            RetrievalResult(entry=e, score=1.0, matched_terms=[f"tag:{tag}"], snippet=(e.summary or "")[:300])
            for e in self.entries
            if any(tt.lower() == t for tt in e.tags)
        ]
        return out

    def search_by_title(self, title: str) -> list[RetrievalResult]:
        t = title.lower()
        return [
            RetrievalResult(entry=e, score=1.0, matched_terms=[f"title:{title}"], snippet=(e.summary or "")[:300])
            for e in self.entries
            if e.title.lower() == t
        ]
