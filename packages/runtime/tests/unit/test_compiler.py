"""Unit tests for the wiki compiler."""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.knowledge import frontmatter as fm
from ginno_runtime.knowledge.compiler import (
    WikiCompiler,
    _append_related,
    extract_concepts,
    generate_concept_page,
    sanitize_filename,
)

pytestmark = pytest.mark.unit


def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ---------------- pure helpers ---------------- #
def test_extract_concepts_bold_and_code_dedup():
    text = "The **Router** handles **Router** dispatch and uses `interrupt` here."
    c = extract_concepts(text)
    terms = [x["term"] for x in c]
    assert terms.count("Router") == 1
    assert "Router" in terms and "interrupt" in terms
    # context window present
    assert all(x["context"] for x in c)


def test_sanitize_filename():
    assert sanitize_filename("Hello World") == "hello-world"
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert sanitize_filename("   ") == "untitled"
    assert len(sanitize_filename("x" * 200)) <= 80


def test_generate_concept_page_has_frontmatter_and_related():
    page = generate_concept_page("Router", "ctx text", "Raw/a.md", ["arch"], ["Other"])
    meta, body = fm.split_frontmatter(page)
    assert meta["title"] == "Router"
    assert "arch" in meta["tags"]
    assert meta["sources"] == ["Raw/a.md"]
    assert "# Router" in body
    assert "## Related" in body
    assert "[[Other]]" in body


def test_append_related_is_idempotent(tmp_path):
    p = tmp_path / "x.md"
    _write(p, "---\ntitle: X\n---\n# X\n\nbody\n\n## Related\n\n- [[A]]\n")
    added = _append_related(p, ["A", "B"])
    assert added == ["B"]
    text = p.read_text(encoding="utf-8")
    assert text.count("[[A]]") == 1
    assert "[[B]]" in text
    # calling again adds nothing
    assert _append_related(p, ["A", "B"]) == []


# ---------------- build_all ---------------- #
def _doc(tags, *concepts):
    body_lines = ["intro paragraph that is long enough to be a summary yes indeed."]
    for c in concepts:
        body_lines.append(f"We rely on **{c}** to do the routing work here.")
    fm_tags = ", ".join(tags)
    return "---\ntitle: doc\ntags: [" + fm_tags + "]\n---\n\n# doc\n\n" + "\n\n".join(body_lines) + "\n"


def test_build_all_creates_concepts_summary_and_index(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "Ginno" / "Raw" / "d1.md", _doc(["arch", "permission"], "权限节点", "AlphaX"))
    _write(vault / "Ginno" / "Raw" / "d2.md", _doc(["arch", "permission"], "权限节点", "BetaY"))
    comp = WikiCompiler(vault)
    res = comp.build_all()

    assert res["scanned"] == 2
    wiki = vault / "Ginno" / "Wiki"
    # concept pages exist
    assert (wiki / "concepts" / "权限节点.md").exists()
    assert (wiki / "concepts" / "alphax.md").exists()
    assert (wiki / "concepts" / "betay.md").exists()
    # shared concept merged both sources on recompile
    meta, _ = fm.split_frontmatter((wiki / "concepts" / "权限节点.md").read_text(encoding="utf-8"))
    assert len(meta["sources"]) == 2
    # per-doc summary pages
    assert (wiki / "doc.md").exists()
    # INDEX regenerated and references a concept
    index = (wiki / "INDEX.md").read_text(encoding="utf-8")
    assert "Wiki Index" in index and "权限节点" in index
    # discovered associations recorded (cross-concept edges exist via shared tags)
    assert isinstance(res["discovered"], list)


def test_build_all_excludes_wiki_output_from_raw(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "Ginno" / "Raw" / "d1.md", _doc(["arch"], "Foo"))
    comp = WikiCompiler(vault)
    comp.build_all()
    # second build: no raw files changed but wiki outputs must not be recompiled as raw
    res2 = comp.build_all()
    assert res2["scanned"] == 1  # only the single raw doc, not the wiki pages


# ---------------- auto-associate (deterministic) ---------------- #
def test_auto_associate_writes_high_score_related(tmp_path):
    vault = tmp_path / "vault"
    concepts = vault / "Ginno" / "Wiki" / "concepts"
    ident = "identical routing summary sentence about nodes and edges here"
    _write(
        concepts / "alphax.md",
        f"---\ntitle: AlphaX\ntags: [t1, t2]\nsources:\n  - Raw/one.md\n---\n# AlphaX\n\n{ident}\n\n## Related\n\n",
    )
    _write(
        concepts / "betay.md",
        f"---\ntitle: BetaY\ntags: [t1, t2]\nsources:\n  - Raw/two.md\n---\n# BetaY\n\n{ident}\n\n## Related\n\n",
    )
    # a page that links to both makes co-occurrence push the pair over the 0.7 threshold
    _write(
        vault / "Ginno" / "Wiki" / "bridge.md",
        "---\ntitle: bridge\ntags: [z]\n---\n# bridge\n\nsee [[AlphaX]] and [[BetaY]].\n",
    )
    comp = WikiCompiler(vault)
    res = comp._auto_associate(["AlphaX", "BetaY"])
    pairs = {(l["from"], l["to"]) for l in res.new_links}
    assert ("AlphaX", "BetaY") in pairs and ("BetaY", "AlphaX") in pairs
    assert "[[BetaY]]" in (concepts / "alphax.md").read_text(encoding="utf-8")
    # and the discovery records mark them auto-applied
    auto = {d["page"]: d["related_to"] for d in res.discovered if d["autoApplied"]}
    assert auto.get("AlphaX") == "BetaY"
