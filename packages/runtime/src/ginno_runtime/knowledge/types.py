"""Knowledge / LLMWiki data models.

A wiki entry is just an Obsidian markdown file with YAML frontmatter. The index
is an in-memory list of :class:`WikiEntry` (no database, no embeddings by
default) rebuilt periodically from the vault on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WikiEntry:
    """One indexed knowledge page (a markdown file in the vault)."""

    path: str                      # absolute path
    relative_path: str             # relative to vault root
    title: str
    summary: str
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)   # [[wikilinks]] found in body
    modified: float = 0.0          # mtime (seconds)
    checksum: str = ""             # sha256 of raw content
    type: str | None = None
    confidence: str | None = None  # high | medium | low
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relative_path": self.relative_path,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "links": self.links,
            "modified": self.modified,
            "type": self.type,
            "confidence": self.confidence,
            "sources": self.sources,
        }


@dataclass
class RetrievalResult:
    """A scored hit returned by the retriever."""

    entry: WikiEntry
    score: float
    matched_terms: list[str] = field(default_factory=list)
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.entry.title,
            "path": self.entry.relative_path,
            "tags": self.entry.tags,
            "summary": self.snippet,
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms,
        }


@dataclass
class KnowledgeConfig:
    """The `knowledge` block of settings.json, with defaults merged in."""

    enabled: bool = False
    vault_path: str = ""
    raw_dir: str = "Ginno/Raw"
    wiki_dir: str = "Ginno/Wiki"
    research_dir: str = "Ginno/Research"
    auto_inject: bool = True
    inject_top_k: int = 5
    inject_min_score: float = 0.3
    rescan_interval_s: int = 60
    use_semantic: bool = False
    # memory refinery
    capture: bool = True
    auto_summarize: bool = True
    pool_flush_threshold: int = 30
    summarize_model: str = ""
    memory_budget_chars: int = 3000

    @property
    def usable(self) -> bool:
        """True when the subsystem can actually operate (enabled + vault set)."""
        return self.enabled and bool(self.vault_path)
