"""Unit tests for the vault indexer (scan / incremental / accessors)."""

from __future__ import annotations

import time

import pytest

from ginno_runtime.knowledge.indexer import WikiIndexer, get_indexer, parse_file, reset_indexers

pytestmark = pytest.mark.unit


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_scan_indexes_markdown_only(tmp_path):
    _write(tmp_path / "a.md", "---\ntitle: A\n---\nbody")
    _write(tmp_path / "b.markdown", "# B\nbody")
    _write(tmp_path / "c.txt", "not markdown")
    idx = WikiIndexer(tmp_path)
    assert idx.scan() == 2
    assert {e.title for e in idx.get_entries()} == {"A", "B"}


def test_title_fallback_chain(tmp_path):
    # frontmatter title wins
    _write(tmp_path / "1.md", "---\ntitle: FM Title\n---\n# H1\nbody")
    # else first H1
    _write(tmp_path / "2.md", "# H1 Title\nbody")
    # else filename stem
    _write(tmp_path / "stem-name.md", "no heading, no frontmatter")
    idx = WikiIndexer(tmp_path)
    idx.scan()
    by_rel = {e.relative_path: e.title for e in idx.get_entries()}
    assert by_rel["1.md"] == "FM Title"
    assert by_rel["2.md"] == "H1 Title"
    assert by_rel["stem-name.md"] == "stem-name"


def test_skip_dirs(tmp_path):
    _write(tmp_path / "good.md", "# Good")
    _write(tmp_path / ".obsidian" / "x.md", "# Skip")
    _write(tmp_path / "node_modules" / "y.md", "# Skip")
    _write(tmp_path / ".hidden" / "z.md", "# Skip")
    idx = WikiIndexer(tmp_path)
    idx.scan()
    assert [e.title for e in idx.get_entries()] == ["Good"]


def test_backlinks_and_orphans(tmp_path):
    _write(tmp_path / "a.md", "---\ntitle: Alpha\n---\nlinks to [[Beta]]")
    _write(tmp_path / "b.md", "---\ntitle: Beta\n---\nstandalone")
    idx = WikiIndexer(tmp_path)
    idx.scan()
    assert idx.get_backlinks("Beta") == ["Alpha"]
    orphans = {e.title for e in idx.get_orphans()}
    assert "Beta" not in orphans  # Beta has an incoming link
    assert "Alpha" in orphans     # Alpha has none


def test_incremental_scan_detects_changes(tmp_path):
    _write(tmp_path / "a.md", "# A\none")
    idx = WikiIndexer(tmp_path)
    idx.scan()
    assert idx.incremental_scan() == {"added": 0, "updated": 0, "removed": 0}

    _write(tmp_path / "b.md", "# B\ntwo")          # add
    diff = idx.incremental_scan()
    assert diff["added"] == 1

    # modify (bump mtime + content)
    time.sleep(0.01)
    _write(tmp_path / "a.md", "# A\nchanged content")
    diff = idx.incremental_scan()
    assert diff["updated"] == 1

    (tmp_path / "b.md").unlink()                    # remove
    diff = idx.incremental_scan()
    assert diff["removed"] == 1


def test_checksum_and_modified_set(tmp_path):
    _write(tmp_path / "a.md", "# A\nbody")
    e = parse_file(tmp_path / "a.md", tmp_path)
    assert e.checksum and len(e.checksum) == 64
    assert e.modified > 0
    assert e.relative_path == "a.md"


def test_get_indexer_shared_and_reset(tmp_path):
    _write(tmp_path / "a.md", "# A")
    reset_indexers()
    i1 = get_indexer(str(tmp_path))
    i2 = get_indexer(str(tmp_path))
    assert i1 is i2  # cached
    reset_indexers()
    i3 = get_indexer(str(tmp_path))
    assert i3 is not i1
