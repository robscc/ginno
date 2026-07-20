"""Unit test: indexer include_dirs scopes the corpus to a subtree."""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.knowledge.indexer import WikiIndexer

pytestmark = pytest.mark.unit


def _w(p: Path, title: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: {title}\n---\n# {title}\n\nbody text here.\n", encoding="utf-8")


def test_include_dirs_only_indexes_those(tmp_path):
    _w(tmp_path / "wiki" / "concepts" / "a.md", "WikiA")
    _w(tmp_path / "wiki" / "b.md", "WikiB")
    _w(tmp_path / "raw" / "r.md", "RawDoc")
    _w(tmp_path / "research" / "x.md", "ResearchDoc")
    _w(tmp_path / "loose.md", "LooseDoc")

    idx = WikiIndexer(tmp_path, include_dirs=["wiki"])
    idx.scan()
    titles = {e.title for e in idx.get_entries()}
    assert titles == {"WikiA", "WikiB"}

    # empty include_dirs => no filter => whole vault
    idx2 = WikiIndexer(tmp_path)
    idx2.scan()
    assert {e.title for e in idx2.get_entries()} == {"WikiA", "WikiB", "RawDoc", "ResearchDoc", "LooseDoc"}
