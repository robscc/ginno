"""Association engine — auto-discover related pages without embeddings.

Pairwise signals (mirrors the design doc / Molly's WikiLLM):

    semantic    0.35   TF-IDF cosine over a body proxy (title + summary + tags)
    tag_overlap 0.25   Jaccard of tag sets
    co_occur    0.20   co-citation: Jaccard of the pages that link to both
    temporal    0.10   exp(-|Δmtime| / 7d), counted only when > 0.3
    hierarchy   0.10   one page mentions the other's title, covers >=50% of its
                       tags and is >=1.5x its size (either direction)

``score = Σ(signal·weight)`` capped at 1.0; an edge is kept when ``score >= 0.3``.
``discover()`` additionally returns strong edges (>=0.8), clusters, orphan
bridges and merge candidates (semantic·0.5 + tag_overlap·0.5 >= 0.75).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from .tokenize import tokenize_query
from .types import WikiEntry

WEIGHTS = {
    "semantic": 0.35,
    "tag_overlap": 0.25,
    "co_occur": 0.20,
    "temporal": 0.10,
    "hierarchy": 0.10,
}
TAU_SECONDS = 7 * 86400
MIN_EDGE = 0.3
STRONG = 0.8
CLUSTER_EDGE = 0.5
CLUSTER_DENSITY = 0.4
MERGE = 0.75


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", (s or "").lower())


def _proxy(e: WikiEntry) -> str:
    return " ".join([e.title or "", e.summary or "", " ".join(e.tags or [])])


def _jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    u = len(sa | sb)
    return len(sa & sb) / u if u else 0.0


def _cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _build_tfidf(entries: list[WikiEntry]) -> dict[str, dict[str, float]]:
    """Sparse TF-IDF vectors keyed by entry path (TF normalized by doc length)."""
    doc_tokens = {e.path: tokenize_query(_proxy(e)) for e in entries}
    df: dict[str, int] = {}
    for toks in doc_tokens.values():
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(n / v) + 1 for n in [len(entries)] for t, v in df.items()} if entries else {}
    vecs: dict[str, dict[str, float]] = {}
    for e in entries:
        toks = doc_tokens[e.path]
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        length = len(toks) or 1
        vecs[e.path] = {t: (c / length) * idf.get(t, 0.0) for t, c in tf.items()}
    return vecs


@dataclass
class Association:
    a: str
    b: str
    score: float
    dominant: str
    signals: dict = field(default_factory=dict)


class AssociationEngine:
    def __init__(
        self,
        entries: list[WikiEntry],
        backlinks: Callable[[str], list[str]] | None = None,
        orphans: set[str] | None = None,
    ) -> None:
        self.entries = list(entries)
        self._by_title = {e.title.lower(): e for e in self.entries}
        known = set(self._by_title)
        self._back = {
            t: {b.lower() for b in (backlinks(t) if backlinks else [])}
            for t in known
        }
        self._fwd = {
            t: {lnk.lower() for lnk in (e.links or []) if lnk.lower() in known}
            for t, e in self._by_title.items()
        }
        self._orphans = {o.lower() for o in (orphans or set())}
        self._vecs = _build_tfidf(self.entries) if self.entries else {}
        self.edges: list[Association] = []
        self._build()

    # ---- per-entry helpers ----
    def _size(self, e: WikiEntry) -> int:
        return len(e.summary or "") + len(e.title or "") + sum(len(t) for t in (e.tags or []))

    def _skip(self, ea: WikiEntry, eb: WikiEntry) -> bool:
        if ea.path == eb.path:
            return True
        na, nb = _norm(ea.title), _norm(eb.title)
        if na and nb and (na in nb or nb in na):
            return True
        if ea.sources and eb.sources and set(ea.sources) & set(eb.sources):
            return True  # siblings compiled from the same raw doc
        # parent/child: one page's source is the other page's file
        for s in ea.sources or []:
            if _norm(s.rsplit("/", 1)[-1].rsplit(".", 1)[0]) and _norm(
                s.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            ) == nb:
                return True
        for s in eb.sources or []:
            if _norm(s.rsplit("/", 1)[-1].rsplit(".", 1)[0]) == na:
                return True
        # already explicitly linked (either direction)
        if eb.title.lower() in self._fwd.get(ea.title.lower(), set()):
            return True
        if ea.title.lower() in self._fwd.get(eb.title.lower(), set()):
            return True
        return False

    def _hierarchy(self, ea: WikiEntry, eb: WikiEntry) -> float:
        def direction(parent: WikiEntry, child: WikiEntry) -> float:
            ct = _norm(child.title)
            if not ct or ct not in (_norm(parent.summary) + _norm(parent.title)):
                return 0.0
            ctags = {t.lower() for t in (child.tags or [])}
            ptags = {t.lower() for t in (parent.tags or [])}
            cover = (len(ptags & ctags) / len(ctags)) if ctags else 0.0
            if cover < 0.5:
                return 0.0
            if self._size(parent) < 1.5 * self._size(child):
                return 0.0
            return 1.0

        return max(direction(ea, eb), direction(eb, ea))

    def _signals(self, ea: WikiEntry, eb: WikiEntry) -> dict[str, float]:
        s: dict[str, float] = {
            "semantic": _cosine(self._vecs.get(ea.path, {}), self._vecs.get(eb.path, {})),
            "tag_overlap": _jaccard(
                [t.lower() for t in (ea.tags or [])], [t.lower() for t in (eb.tags or [])]
            ),
            "co_occur": _jaccard(
                self._back.get(ea.title.lower(), set()), self._back.get(eb.title.lower(), set())
            ),
        }
        dt = abs((ea.modified or 0) - (eb.modified or 0))
        t = math.exp(-dt / TAU_SECONDS)
        s["temporal"] = t if t > 0.3 else 0.0
        s["hierarchy"] = self._hierarchy(ea, eb)
        return s

    def _build(self) -> None:
        es = self.entries
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                ea, eb = es[i], es[j]
                if self._skip(ea, eb):
                    continue
                sig = self._signals(ea, eb)
                score = min(1.0, sum(sig[k] * WEIGHTS[k] for k in WEIGHTS))
                if score < MIN_EDGE:
                    continue
                dom = max(sig, key=lambda k: sig[k])
                self.edges.append(Association(ea.title, eb.title, round(score, 4), dom, sig))

    # ---- public queries ----
    def find_related(self, title: str, top_k: int = 10) -> dict:
        t = title.lower()
        rel = []
        for e in self.edges:
            other = e.b if e.a.lower() == t else (e.a if e.b.lower() == t else None)
            if other:
                rel.append({"title": other, "score": e.score, "type": e.dominant, "signals": e.signals})
        rel.sort(key=lambda x: x["score"], reverse=True)
        clusters = [c for c in self.clusters() if any(m.lower() == t for m in c["members"])]
        return {"related": rel[:top_k], "clusters": clusters}

    def clusters(self) -> list[dict]:
        adj: dict[str, set[str]] = {}
        for e in self.edges:
            if e.score < CLUSTER_EDGE:
                continue
            adj.setdefault(e.a, set()).add(e.b)
            adj.setdefault(e.b, set()).add(e.a)
        seen: set[str] = set()
        out: list[dict] = []
        for n in adj:
            if n in seen:
                continue
            comp: list[str] = []
            stack = [n]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                comp.append(x)
                stack.extend(adj.get(x, set()))
            if len(comp) < 2:
                continue
            members = set(comp)
            internal = sum(
                1 for e in self.edges if e.a in members and e.b in members and e.score >= CLUSTER_EDGE
            )
            max_e = len(members) * (len(members) - 1) / 2
            density = internal / max_e if max_e else 0.0
            if density < CLUSTER_DENSITY:
                continue
            tagc: dict[str, int] = {}
            for m in comp:
                ent = self._by_title.get(m.lower())
                if ent:
                    for tg in ent.tags or []:
                        tagc[tg] = tagc.get(tg, 0) + 1
            label = " + ".join(t for t, _ in sorted(tagc.items(), key=lambda kv: -kv[1])[:3]) or "cluster"
            out.append({"label": label, "members": sorted(comp), "density": round(density, 3)})
        return out

    def discover(self) -> dict:
        strong = sorted(
            (
                {"a": e.a, "b": e.b, "score": e.score, "type": e.dominant}
                for e in self.edges
                if e.score >= STRONG
            ),
            key=lambda x: -x["score"],
        )
        connected = {e.a.lower() for e in self.edges} | {e.b.lower() for e in self.edges}
        isolated = [e.title for e in self.entries if e.title.lower() not in connected]
        orphan_bridges = [
            {"a": e.a, "b": e.b, "score": e.score, "type": e.dominant}
            for e in self.edges
            if e.a.lower() in self._orphans or e.b.lower() in self._orphans
        ]
        merge_candidates = sorted(
            (
                {"a": e.a, "b": e.b, "score": round(e.signals["semantic"] * 0.5 + e.signals["tag_overlap"] * 0.5, 4)}
                for e in self.edges
                if e.signals["semantic"] * 0.5 + e.signals["tag_overlap"] * 0.5 >= MERGE
            ),
            key=lambda x: -x["score"],
        )
        return {
            "strong": strong,
            "clusters": self.clusters(),
            "isolated": isolated,
            "orphan_bridges": orphan_bridges,
            "merge_candidates": merge_candidates,
            "stats": {"pages": len(self.entries), "edges": len(self.edges)},
        }


# ---- shared, cached engines (5-min TTL; invalidated after a build) ----
import time as _time

_ENGINES: dict[str, tuple[float, AssociationEngine]] = {}
_TTL = 300


def get_engine(indexer, orphans: set[str] | None = None, force: bool = False) -> AssociationEngine:
    key = str(getattr(indexer, "vault_root", indexer))
    now = _time.time()
    cached = _ENGINES.get(key)
    if not force and cached and (now - cached[0]) < _TTL:
        return cached[1]
    eng = AssociationEngine(
        indexer.get_entries(),
        backlinks=indexer.get_backlinks,
        orphans=orphans if orphans is not None else {e.title for e in indexer.get_orphans()},
    )
    _ENGINES[key] = (now, eng)
    return eng


def reset_engines() -> None:
    _ENGINES.clear()
