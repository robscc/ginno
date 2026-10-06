"""Unit tests for the workflow python entries (workflows/scripts/kb_wiki.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime.workflows.scripts import ENTRY_REGISTRY
from ginno_runtime.workflows.scripts.kb_wiki import (
    kb_wiki_finalize,
    kb_wiki_inventory,
    kb_wiki_lint,
    _slug,
)

pytestmark = pytest.mark.unit

RAW_BODY = (
    "---\ntitle: Test 报告\ntags: [t1]\n---\n\n# Test 报告\n\n"
    "今日合计 7,625 stars,主线是 AI Agent 沉淀。\n"
)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    raw = vault / "Ginno" / "Raw"
    raw.mkdir(parents=True)
    (raw / "r.md").write_text(RAW_BODY, encoding="utf-8")
    sp = tmp_path / "settings.json"
    sp.write_text(
        json.dumps(
            {
                "knowledge": {
                    "enabled": True,
                    "vault_path": str(vault),
                    "raw_dir": "Ginno/Raw",
                    "wiki_dir": "Ginno/Wiki",
                }
            }
        ),
        encoding="utf-8",
    )
    return vault


def test_entries_registered():
    for name in ("kb_wiki_inventory", "kb_wiki_finalize", "kb_wiki_lint"):
        assert name in ENTRY_REGISTRY


def test_inventory_lists_new_then_finalize_marks(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))  # 台账落到隔离 home
    vault = _vault(tmp_path)

    plan = kb_wiki_inventory({})
    assert plan["raw_total"] == 1
    assert len(plan["files"]) == 1
    f = plan["files"][0]
    assert f["rel"] == "Ginno/Raw/r.md" and f["reason"] == "new"
    assert f["title"] == "Test 报告"

    # 模拟 agent 写出 wiki 页后 finalize:落台账 + INDEX + lint
    wiki = vault / "Ginno" / "Wiki"
    wiki.mkdir()
    (wiki / "test-报告.md").write_text(
        '---\ntitle: "Test 报告"\nconfidence: medium\nsources:\n  - "Ginno/Raw/r.md"\n'
        "---\n\n# Test 报告\n\n综合内容。\n",
        encoding="utf-8",
    )
    rep = kb_wiki_finalize({"compiled": [{"doc": "Ginno/Raw/r.md", "page": "Test 报告"}]})
    assert rep["marked"] == ["Ginno/Raw/r.md"]
    assert rep["indexed"] >= 1
    assert rep["broken"] == []
    assert (wiki / "INDEX.md").exists()

    # 再次 inventory:已编译且页在 → 不再列出
    assert kb_wiki_inventory({})["files"] == []


def test_inventory_flags_changed_and_missing_page(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    vault = _vault(tmp_path)
    rep = kb_wiki_finalize({"compiled": [{"doc": "Ginno/Raw/r.md", "page": "Test 报告"}]})
    assert rep["marked"]

    # raw 变更 → reason=changed
    (vault / "Ginno" / "Raw" / "r.md").write_text(RAW_BODY + "\n追加一段。\n", encoding="utf-8")
    files = kb_wiki_inventory({})["files"]
    assert len(files) == 1 and files[0]["reason"] == "changed"

    # 重新落台账;页被删 → reason=missing_page
    wiki = vault / "Ginno" / "Wiki"
    (wiki / "test-报告.md").write_text(
        '---\ntitle: "Test 报告"\nsources:\n  - "Ginno/Raw/r.md"\n---\n\n# Test 报告\n\nv2。\n',
        encoding="utf-8",
    )
    kb_wiki_finalize({"compiled": [{"doc": "Ginno/Raw/r.md", "page": "Test 报告"}]})
    (wiki / "test-报告.md").unlink()
    files = kb_wiki_inventory({})["files"]
    assert len(files) == 1 and files[0]["reason"] == "missing_page"


def test_lint_reports_dangling_links(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    vault = _vault(tmp_path)
    wiki = vault / "Ginno" / "Wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text(
        '---\ntitle: "A"\n---\n\n# A\n\nsee [[Ghost]] and [[B]].\n', encoding="utf-8"
    )
    (wiki / "b.md").write_text('---\ntitle: "B"\n---\n\n# B\n\nback to [[a]].\n', encoding="utf-8")
    broken = kb_wiki_lint({})["broken"]
    assert broken == [{"page": "A", "link": "Ghost"}]


def test_slug_matches_page_naming():
    assert _slug("Hello World") == "hello-world"
    assert _slug("53%") == "53"
    assert _slug("   ") == "untitled"
    assert len(_slug("x" * 200)) <= 80
