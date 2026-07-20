"""Vault indexer — scan an Obsidian vault into an in-memory list of WikiEntry.

No database and no embeddings: the index is a plain list rebuilt periodically
from disk. ``mtime`` is used as a fast unchanged check and a SHA-256 ``checksum``
confirms real changes during incremental scans (mirrors Molly's WikiIndexer).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from . import frontmatter as fm
from .types import WikiEntry

SKIP_DIRS = {"node_modules", ".obsidian", ".trash", ".git", ".vscode", ".ginno", ".molly"}
INDEX_EXTENSIONS = {".md", ".markdown"}


def _iter_markdown_files(
    root: Path,
    exclude_roots: tuple[Path, ...] = (),
    include_roots: tuple[Path, ...] = (),
):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in INDEX_EXTENSIONS:
                fp = Path(dirpath) / fn
                rfp = fp.resolve() if (exclude_roots or include_roots) else fp
                if include_roots and not any(_is_under(rfp, i) for i in include_roots):
                    continue
                if exclude_roots and any(_is_under(rfp, e) for e in exclude_roots):
                    continue
                yield fp


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def parse_file(path: Path, vault_root: Path) -> WikiEntry | None:
    """Parse one markdown file into a WikiEntry (None on read error)."""
    try:
        raw_bytes = path.read_bytes()
        raw = raw_bytes.decode("utf-8", errors="replace")
        stat = path.stat()
    except OSError:
        return None

    meta, body = fm.split_frontmatter(raw)
    rel = path.relative_to(vault_root).as_posix()

    title = (
        (meta.get("title") or "").strip()
        or fm.extract_title(body)
        or path.stem
        or "Untitled"
    )
    summary = (
        (meta.get("abstract") or meta.get("summary") or "").strip()
        or fm.extract_summary(body)
    )
    return WikiEntry(
        path=str(path),
        relative_path=rel,
        title=title,
        summary=summary,
        tags=fm._as_list(meta.get("tags")),
        links=fm.extract_wikilinks(body),
        modified=stat.st_mtime,
        checksum=hashlib.sha256(raw_bytes).hexdigest(),
        type=meta.get("type"),
        confidence=meta.get("confidence"),
        sources=fm._as_list(meta.get("sources")),
    )


class WikiIndexer:
    """Holds the in-memory index for one vault."""

    def __init__(
        self,
        vault_path: str | Path,
        exclude_dirs: list[str] | None = None,
        include_dirs: list[str] | None = None,
    ) -> None:
        self.vault_root = Path(vault_path).expanduser().resolve()
        self.exclude_roots = tuple(
            (self.vault_root / d).resolve() for d in (exclude_dirs or [])
        )
        # When set, ONLY files under these dirs are indexed (the compiled wiki is
        # the knowledge corpus; raw/research/loose notes stay out).
        self.include_roots = tuple(
            (self.vault_root / d).resolve() for d in (include_dirs or [])
        )
        self.entries: list[WikiEntry] = []
        self._by_path: dict[str, WikiEntry] = {}
        self._backlinks: dict[str, list[str]] = {}   # target title(lower) -> source titles
        self.last_full_scan: float = 0.0

    # ---- scanning ----
    def scan(self) -> int:
        """Full rescan. Returns the number of indexed entries."""
        entries: list[WikiEntry] = []
        if self.vault_root.is_dir():
            for f in _iter_markdown_files(self.vault_root, self.exclude_roots, self.include_roots):
                e = parse_file(f, self.vault_root)
                if e:
                    entries.append(e)
        self.entries = entries
        self._by_path = {e.path: e for e in entries}
        self._build_backlinks()
        self.last_full_scan = time.time()
        return len(entries)

    def incremental_scan(self) -> dict[str, int]:
        """Diff against the current index using mtime then checksum."""
        if not self.vault_root.is_dir():
            self.scan()
            return {"added": 0, "updated": 0, "removed": 0}

        current: dict[str, WikiEntry] = {}
        for f in _iter_markdown_files(self.vault_root, self.exclude_roots, self.include_roots):
            e = parse_file(f, self.vault_root)
            if e:
                current[e.path] = e

        added = updated = 0
        for path, e in current.items():
            old = self._by_path.get(path)
            if old is None:
                added += 1
            elif old.modified != e.modified and old.checksum != e.checksum:
                updated += 1
        removed = sum(1 for path in self._by_path if path not in current)

        if added or updated or removed:
            self.entries = list(current.values())
            self._by_path = current
            self._build_backlinks()
        return {"added": added, "updated": updated, "removed": removed}

    def refresh(self, interval_s: int) -> None:
        """Full scan on first call or when stale, else incremental."""
        if not self.last_full_scan or (time.time() - self.last_full_scan) > interval_s:
            self.scan()
        else:
            self.incremental_scan()

    def _build_backlinks(self) -> None:
        backlinks: dict[str, list[str]] = {}
        titles = {e.title.lower() for e in self.entries}
        for e in self.entries:
            for link in e.links:
                key = link.lower()
                # resolve to known titles when possible; else keep the raw target
                if key not in titles:
                    continue
                backlinks.setdefault(key, [])
                if e.title not in backlinks[key]:
                    backlinks[key].append(e.title)
        self._backlinks = backlinks

    # ---- accessors ----
    def get_entries(self) -> list[WikiEntry]:
        return self.entries

    def get_all_tags(self) -> list[str]:
        tags: dict[str, None] = {}
        for e in self.entries:
            for t in e.tags:
                tags.setdefault(t.lower(), None)
        return list(tags.keys())

    def find_by_title(self, title: str) -> WikiEntry | None:
        t = title.lower()
        for e in self.entries:
            if e.title.lower() == t:
                return e
        return None

    def get_backlinks(self, title: str) -> list[str]:
        return self._backlinks.get(title.lower(), [])

    def get_orphans(self) -> list[WikiEntry]:
        """Entries with no incoming links."""
        linked = set(self._backlinks.keys())
        return [e for e in self.entries if e.title.lower() not in linked]


# ---- shared indexers (one per vault), refreshed on an interval ----
_INDEXERS: dict[str, WikiIndexer] = {}


def get_indexer(
    vault_path: str | Path,
    interval_s: int = 60,
    exclude_dirs: list[str] | None = None,
    include_dirs: list[str] | None = None,
) -> WikiIndexer:
    """Return a shared, freshly-refreshed indexer for the given vault."""
    key = str(Path(vault_path).expanduser().resolve())
    idx = _INDEXERS.get(key)
    if idx is None:
        idx = WikiIndexer(key, exclude_dirs=exclude_dirs, include_dirs=include_dirs)
        _INDEXERS[key] = idx
    idx.refresh(interval_s)
    return idx


def reset_indexers() -> None:
    """Clear the shared cache (used by tests)."""
    _INDEXERS.clear()
