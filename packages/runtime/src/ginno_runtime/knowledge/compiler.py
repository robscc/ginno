"""Wiki compiler — deterministic raw → wiki concept pages (no LLM by default).

Mirrors the design doc / Molly's WikiCompiler:

1. ``extract_concepts`` pulls ``**bold**`` and inline ``` `code` ``` spans as
   concepts (with ±50-char context), de-duplicated by lowercased term.
2. summary = first real paragraph (>=20 chars) else first 200 chars.
3. the first 10 concepts become pages under ``<wiki_dir>/concepts/`` (created or
   updated with the new source appended).
4. a per-document summary page ``<wiki_dir>/<name>.md`` links those concepts.
5. auto-associate: re-scan + AssociationEngine; related pages with score >= 0.7
   are written into the page's ``## Related`` section, the rest are suggestions.
6. ``update_index`` regenerates ``<wiki_dir>/INDEX.md`` grouped by directory.

``build_all`` compiles every raw file in the vault *except* the wiki output dir
(and ``INDEX.md`` / skip dirs), so re-running is idempotent.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import frontmatter as fm
from .association import AssociationEngine
from .indexer import SKIP_DIRS, WikiIndexer, _iter_markdown_files, parse_file

_BOLD_RE = re.compile(r"\*\*([^*\n]{2,50})\*\*")
_CODE_RE = re.compile(r"`([^`\n]{2,50})`")
_AUTO_THRESHOLD = 0.7
_MAX_CONCEPTS = 10


@dataclass
class CompileResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    new_links: list[dict] = field(default_factory=list)
    discovered: list[dict] = field(default_factory=list)

    def merge(self, other: "CompileResult") -> None:
        self.created += other.created
        self.updated += other.updated
        self.new_links += other.new_links
        self.discovered += other.discovered


def extract_concepts(text: str) -> list[dict]:
    """Concepts from bold + inline-code spans, with ±50-char context, deduped."""
    seen: set[str] = set()
    out: list[dict] = []
    for rx in (_BOLD_RE, _CODE_RE):
        for m in rx.finditer(text or ""):
            term = m.group(1).strip()
            key = term.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 50)
            out.append({"term": term, "context": text[start:end].strip()})
    return out


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "")
    s = re.sub(r"-+", "-", s.strip().replace(" ", "-")).strip("-").lower()
    return (s or "untitled")[:80]


def _summary(body: str) -> str:
    s = fm.extract_summary(body or "")
    if len(s) < 20:
        s = (body or "").strip().replace("\n", " ")[:200]
    return s


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_concept_page(
    term: str, context: str, source_rel: str, tags: list[str], related: list[str]
) -> str:
    tagstr = ", ".join(_q(t) for t in (tags or []))
    rel = "\n".join(f"- [[{r}]]" for r in (related or []))
    return (
        "---\n"
        f"title: {_q(term)}\n"
        f"date: {_q(_iso_now())}\n"
        f"tags: [{tagstr}]\n"
        "sources:\n"
        f"  - {_q(source_rel)}\n"
        "confidence: medium\n"
        "---\n\n"
        f"# {term}\n\n"
        f"{context}\n\n"
        "## Related\n\n"
        f"{rel}\n"
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _append_related(path: Path, titles: list[str]) -> list[str]:
    """Add ``- [[title]]`` lines under the ``## Related`` heading; return added."""
    if not path.exists() or not titles:
        return []
    text = path.read_text(encoding="utf-8")
    marker = "## Related"
    if marker not in text:
        text = text.rstrip() + f"\n\n{marker}\n"
    head, _, tail = text.partition(marker)
    added = [t for t in titles if f"[[{t}]]" not in tail]
    if not added:
        return []
    block = "\n".join(f"- [[{t}]]" for t in added)
    new_tail = (tail.rstrip("\n") + "\n" + block + "\n") if tail.strip() else ("\n" + block + "\n")
    path.write_text(head + marker + new_tail, encoding="utf-8")
    return added


class WikiCompiler:
    def __init__(self, vault_path: str | Path, wiki_dir: str = "Ginno/Wiki", raw_dir: str = "Ginno/Raw") -> None:
        self.vault_root = Path(vault_path).expanduser().resolve()
        self.wiki_dir = (self.vault_root / wiki_dir).resolve()
        self.wiki_dir_rel = wiki_dir  # vault-relative, for include-scoped indexing
        self.raw_dir_name = raw_dir  # informational

    # ---- helpers ----
    def _is_wiki_output(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.wiki_dir)
            return True
        except ValueError:
            return False

    def _raw_files(self) -> list[Path]:
        # Compile only the raw sources dir (e.g. Molly/Raw). If it doesn't exist,
        # fall back to the whole vault minus wiki (legacy / loose-notes layout) so a
        # Build on a vault like Molly never recompiles Research/Todo/Memory/loose notes.
        raw_root = self.vault_root / self.raw_dir_name
        base = raw_root if raw_root.is_dir() else self.vault_root
        out = []
        for f in _iter_markdown_files(base):
            if self._is_wiki_output(f):
                continue
            if f.name == "INDEX.md":
                continue
            out.append(f)
        return out

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.vault_root).as_posix()

    # ---- compile one raw file ----
    def _compile_one(self, raw_path: str | Path) -> tuple[CompileResult, list[str]]:
        """Compile one raw file → (result, produced titles) WITHOUT associating.

        Association is split out so ``build_all`` can run a single graph pass over
        every produced page instead of one O(N²) rescan+rebuild *per raw file*.
        """
        res = CompileResult()
        rp = Path(raw_path)
        if not rp.exists():
            return res, []
        raw = rp.read_text(encoding="utf-8", errors="replace")
        meta, body = fm.split_frontmatter(raw)
        source_rel = self._rel(rp)
        file_title = (meta.get("title") or "").strip() or fm.extract_title(body) or rp.stem
        summary = _summary(body)
        source_tags = fm._as_list(meta.get("tags"))
        concepts = extract_concepts(body)[:_MAX_CONCEPTS]

        concepts_dir = self.wiki_dir / "concepts"
        concept_titles: list[str] = []
        for c in concepts:
            term = c["term"]
            concept_titles.append(term)
            cpath = concepts_dir / f"{sanitize_filename(term)}.md"
            if cpath.exists():
                existing = cpath.read_text(encoding="utf-8")
                emeta, _ = fm.split_frontmatter(existing)
                srcs = fm._as_list(emeta.get("sources"))
                if source_rel not in srcs:
                    srcs.append(source_rel)
                    new_fm = self._rewrite_sources(existing, srcs)
                    _write(cpath, new_fm)
                    res.updated.append(self._rel(cpath))
            else:
                _write(cpath, generate_concept_page(term, c["context"], source_rel, source_tags, []))
                res.created.append(self._rel(cpath))

        # per-document summary page
        spath = self.wiki_dir / f"{sanitize_filename(file_title)}.md"
        key_lines = "\n".join(f"- [[{t}]]" for t in concept_titles)
        summary_page = (
            "---\n"
            f"title: {_q(file_title)}\n"
            f"date: {_q(_iso_now())}\n"
            f"tags: [{', '.join(_q(t) for t in source_tags)}]\n"
            "type: summary\n"
            "sources:\n"
            f"  - {_q(source_rel)}\n"
            "---\n\n"
            f"# {file_title}\n\n"
            f"{summary}\n\n"
            "## Key Concepts\n\n"
            f"{key_lines}\n"
        )
        if spath.exists():
            _write(spath, summary_page)
            res.updated.append(self._rel(spath))
        else:
            _write(spath, summary_page)
            res.created.append(self._rel(spath))

        return res, concept_titles + [file_title]

    def compile(self, raw_path: str | Path) -> CompileResult:
        """Compile one raw file and auto-associate its pages (single-file API)."""
        res, titles = self._compile_one(raw_path)
        res.merge(self._auto_associate(titles))
        return res

    def _rewrite_sources(self, text: str, sources: list[str]) -> str:
        meta, body = fm.split_frontmatter(text)
        meta = dict(meta or {})
        meta["sources"] = sources
        return self._dump_frontmatter(meta) + body

    @staticmethod
    def _dump_frontmatter(meta: dict) -> str:
        lines = ["---"]
        for k, v in meta.items():
            if k == "sources":
                lines.append("sources:")
                for s in v:
                    lines.append(f"  - {_q(s)}")
            elif k == "tags":
                lines.append(f"tags: [{', '.join(_q(t) for t in (v or []))}]")
            elif isinstance(v, str):
                lines.append(f"{k}: {_q(v)}")
            else:
                lines.append(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}")
        lines.append("---\n\n")
        return "\n".join(lines)

    def _auto_associate(self, titles: list[str]) -> CompileResult:
        res = CompileResult()
        # scope to compiled wiki pages only: auto-association (and its Related
        # writes) must only touch compiled wiki pages, never the user's raw docs.
        idx = WikiIndexer(self.vault_root, include_dirs=[self.wiki_dir_rel])
        idx.scan()
        by_path = {e.path: e for e in idx.get_entries()}
        eng = AssociationEngine(
            idx.get_entries(),
            backlinks=idx.get_backlinks,
            orphans={e.title for e in idx.get_orphans()},
        )
        for title in titles:
            ent = idx.find_by_title(title)
            if not ent:
                continue
            rel = eng.find_related(title, top_k=5)
            for r in rel["related"]:
                auto = r["score"] >= _AUTO_THRESHOLD
                res.discovered.append(
                    {
                        "page": title,
                        "related_to": r["title"],
                        "score": r["score"],
                        "type": r["type"],
                        "autoApplied": auto,
                    }
                )
                if not auto:
                    continue
                # write [[related]] into THIS page's Related section
                added = _append_related(Path(ent.path), [r["title"]])
                for a in added:
                    res.new_links.append({"from": title, "to": a})
        return res

    # ---- index + whole-vault build ----
    def update_index(self) -> str:
        idx = WikiIndexer(self.vault_root, include_dirs=[self.wiki_dir_rel])
        idx.scan()
        groups: dict[str, list] = {}
        for e in idx.get_entries():
            if not self._is_wiki_output(Path(e.path)):
                continue
            rel = Path(e.relative_path).as_posix()
            grp = str(Path(rel).parent) if "/" in rel else "(root)"
            groups.setdefault(grp, []).append(e)
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
        _write(self.wiki_dir / "INDEX.md", text)
        return text

    def build_all(self) -> dict:
        t0 = time.time()
        agg = CompileResult()
        scanned = 0
        all_titles: list[str] = []
        for f in self._raw_files():
            scanned += 1
            res, titles = self._compile_one(f)
            agg.merge(res)
            all_titles.extend(titles)
        # One association pass over every produced page. The old per-file pass
        # did R full vault rescans + R O(N²) graph rebuilds → minutes on a vault.
        agg.merge(self._auto_associate(all_titles))
        self.update_index()
        return {
            "scanned": scanned,
            "created": agg.created,
            "updated": agg.updated,
            "new_links": agg.new_links,
            "discovered": agg.discovered,
            "duration_ms": int((time.time() - t0) * 1000),
        }
