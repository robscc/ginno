"""Read-side wiki maintenance: INDEX regeneration + broken-link lint.

Moved out of the deleted deterministic compiler (agent-wiki-workflow-design.md
§4): wiki pages are now written by the agent workflow, but these two helpers
only *read* pages, so they work no matter who authored them. They back the
``kb_wiki_index`` / ``kb_wiki_lint`` workflow entries and the KB page.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import frontmatter as fm
from .indexer import WikiIndexer

_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _wiki_indexer(vault_path: str | Path, wiki_dir: str) -> WikiIndexer:
    # Association/INDEX/lint stay scoped to the compiled wiki dir — they must
    # never touch the user's raw docs (knowledge-and-wiki-design.md §4.5 note).
    return WikiIndexer(Path(vault_path), include_dirs=[wiki_dir])


def update_index(vault_path: str | Path, wiki_dir: str = "Ginno/Wiki") -> str:
    """Regenerate ``<wiki_dir>/INDEX.md`` grouped by directory; returns text."""
    import json

    vault = Path(vault_path).expanduser().resolve()
    wdir = vault / wiki_dir
    idx = _wiki_indexer(vault, wiki_dir)
    idx.scan()
    groups: dict[str, list] = {}
    for e in idx.get_entries():
        rel = Path(e.relative_path).as_posix()
        grp = str(Path(rel).parent) if "/" in rel else "(root)"
        groups.setdefault(grp, []).append(e)

    def _q(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)

    out = [
        "---",
        'title: "Wiki Index"',
        "permission: public",
        "---",
        "",
        "# Wiki Index",
        "",
        f"_Auto-generated. {sum(len(v) for v in groups.values())} pages._",
        "",
    ]
    for grp in sorted(groups):
        out.append(f"## {grp}")
        out.append("")
        for e in sorted(groups[grp], key=lambda x: x.title.lower()):
            tags = f" _({', '.join(e.tags)})_" if e.tags else ""
            out.append(f"- [[{e.relative_path}|{e.title}]]{tags}")
        out.append("")
    text = "\n".join(out)
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "INDEX.md").write_text(text, encoding="utf-8")
    return text


def lint_links(vault_path: str | Path, wiki_dir: str = "Ginno/Wiki") -> list[dict]:
    """Broken ``[[wikilinks]]`` across wiki pages, ``[{page, link}, ...]``.

    A Key Concepts link whose target page never materialized is exactly how
    "链接都没有详细内容" happens silently (2026-10-06 incident) — report
    instead. Targets match an indexed page title, vault-relative path, or stem.
    """
    vault = Path(vault_path).expanduser().resolve()
    idx = _wiki_indexer(vault, wiki_dir)
    idx.scan()
    entries = idx.get_entries()
    titles = {e.title for e in entries}
    paths: set[str] = set()
    for e in entries:
        p = Path(e.relative_path).as_posix()
        paths.add(p)
        paths.add(Path(p).stem)
    broken: list[dict] = []
    for e in entries:
        try:
            text = Path(e.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        body = fm.split_frontmatter(text)[1]
        for m in _LINK_RE.finditer(body):
            target = m.group(1).strip()
            if target in titles or target in paths:
                continue
            broken.append({"page": e.title, "link": target})
    return broken
