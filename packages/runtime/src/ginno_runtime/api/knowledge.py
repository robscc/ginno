"""Knowledge base endpoints: MCP-server vault search + LLMWiki.

The LLMWiki half keeps an in-memory vault index + retrieval over an Obsidian
vault (knowledge-and-wiki-design.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from .. import paths
from .. import server_shared as shared
from ..knowledge import compiler as _kb_compiler
from ..knowledge.association import get_engine as _get_kb_engine
from ..knowledge.association import reset_engines as _reset_kb_engines
from ..knowledge.config import load_knowledge_config as _load_kb_cfg
from ..knowledge.indexer import get_indexer as _get_kb_indexer
from ..knowledge.retriever import WikiRetriever as _WikiRetriever
from ..knowledge.semantic import (
    get_semantic_index as _get_kb_semantic,
)
from ..knowledge.semantic import (
    reset_semantic as _reset_kb_semantic,
)

router = APIRouter()


# ---- MCP-server backed search/list -----------------------------------------


@router.get("/api/kb/servers")
async def kb_servers_endpoint() -> list[dict]:
    if not shared._mcp:
        return []
    return [
        {"name": n, "tools": [t.name for t in live.tools]}
        for n, live in shared._mcp._live.items()
    ]


def _server_roots(name: str) -> list[str]:
    """Best-effort root path(s) for a server, read from mcp.json (the filesystem
    server takes its allowed directory as a positional arg)."""
    p = paths.mcp_config_path()
    if not p.exists():
        return []
    try:
        cfg = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return []
    srv = (cfg.get("mcpServers") or {}).get(name, {}) or {}
    return [a for a in (srv.get("args") or []) if isinstance(a, str) and a.startswith("/")]


async def _kb_call_one(live, tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not live.session or not any(t.name == tool_name for t in live.tools):
        return out
    try:
        res = await live.session.call_tool(tool_name, args)
        for c in getattr(res, "content", []) or []:
            t = getattr(c, "text", None)
            if t:
                out.append(t)
    except Exception:
        pass
    return out


async def _kb_call(tool_name: str, args: dict) -> list[str]:
    out: list[str] = []
    if not shared._mcp:
        return out
    for live in shared._mcp._live.values():
        out.extend(await _kb_call_one(live, tool_name, args))
    return out


@router.get("/api/kb/search")
async def kb_search_endpoint(q: str = "") -> dict:
    if not q or not shared._mcp:
        return {"q": q, "results": []}
    results: list[str] = []
    for name, live in shared._mcp._live.items():
        for root in _server_roots(name) or [""]:
            results.extend(await _kb_call_one(live, "search_files", {"path": root, "pattern": q}))
    return {"q": q, "results": results}


@router.get("/api/kb/list")
async def kb_list_endpoint(path: str = "") -> dict:
    if not shared._mcp:
        return {"path": path, "results": []}
    results: list[str] = []
    for name, live in shared._mcp._live.items():
        roots = [path] if path else (_server_roots(name) or [""])
        for root in roots:
            r = await _kb_call_one(live, "list_directory", {"path": root}) or await _kb_call_one(
                live, "directory_tree", {"path": root}
            )
            results.extend(r)
    return {"path": path, "results": results}


# ---- knowledge base / LLMWiki (in-memory vault index + retrieval) ----


def _kb_not_configured(extra: dict | None = None) -> dict:
    return {"ok": False, "error": "knowledge not configured", **(extra or {})}


def _kb_indexer(cfg):
    """Shared indexer over the whole vault minus the raw sources dir and system
    dirs (``SKIP_DIRS`` such as ``.obsidian``). Finished notes anywhere in the
    vault (e.g. a ``股市/`` folder) are searchable and visible; ``raw_dir`` holds
    compile-sources that surface through their compiled wiki pages instead. An
    empty ``raw_dir`` excludes nothing extra. (The compiler's INDEX/association
    graph deliberately stays scoped to ``wiki_dir`` — see compiler.py.)"""
    return _get_kb_indexer(
        cfg.vault_path,
        cfg.rescan_interval_s,
        exclude_dirs=[cfg.raw_dir] if cfg.raw_dir else None,
    )


def _count_md(root) -> int:
    import os

    from ..knowledge.indexer import INDEX_EXTENSIONS, SKIP_DIRS

    if not root.exists():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in INDEX_EXTENSIONS:
                n += 1
    return n


def _detect_wiki_layout(vault) -> dict:
    """Find a `<namespace>/Wiki` (or a root `Wiki`) layout in *vault*."""

    def _sister(ns_dir, name):
        d = (ns_dir / name) if ns_dir else (vault / name)
        return d.relative_to(vault).as_posix() if d.is_dir() else ""

    root_wiki = vault / "Wiki"
    if root_wiki.is_dir():
        ns_dir = None
        namespace = ""
    else:
        ns_dir = None
        namespace = ""
        for child in sorted(vault.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / "Wiki").is_dir():
                    ns_dir = child
                    namespace = child.name
                    break
    wiki_dir = _sister(ns_dir, "Wiki")
    return {
        "namespace": namespace,
        "wiki_dir": wiki_dir,
        "raw_dir": _sister(ns_dir, "Raw"),
        "research_dir": _sister(ns_dir, "Research"),
        "memory_dir": _sister(ns_dir, "Memory"),
        "todo_dir": _sister(ns_dir, "Todo"),
    }


@router.get("/api/kb/wiki/probe")
def kb_wiki_probe(path: str = "") -> dict:
    """Read-only: detect an existing LLM-Wiki layout under *path* and count pages.

    Does NOT write the vault. Used by the import UI to pre-fill config and show
    how many compiled wiki pages / raw docs a vault contains.
    """
    if not path:
        return {"ok": False, "error": "path required"}
    vault = Path(path).expanduser().resolve()
    if not vault.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    layout = _detect_wiki_layout(vault)
    wiki_abs = (vault / layout["wiki_dir"]) if layout["wiki_dir"] else vault
    raw_abs = (vault / layout["raw_dir"]) if layout["raw_dir"] else None
    return {
        "ok": True,
        "vault_path": str(vault),
        "detected": layout,
        "wiki_pages": _count_md(wiki_abs),
        "raw_pages": _count_md(raw_abs) if raw_abs else 0,
        "has_index": (wiki_abs / "INDEX.md").is_file() if layout["wiki_dir"] else False,
        "total_md": _count_md(vault),
    }


@router.get("/api/kb/wiki/search")
async def kb_wiki_search(q: str = "", tag: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"results": []})
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    ret = _WikiRetriever(entries)
    if tag:
        results = ret.search_by_tag(tag)
    else:
        results = ret.retrieve(
            q,
            top_k=10,
            min_score=0.2,
            semantic=_get_kb_semantic(cfg, entries),
            semantic_weight=cfg.semantic_weight,
        )
    return {"ok": True, "q": q, "tag": tag, "results": [r.to_dict() for r in results]}


@router.get("/api/kb/wiki/list")
async def kb_wiki_list() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [
        {
            "title": e.title,
            "path": e.relative_path,
            "tags": e.tags,
            "links": e.links,
            "modified": e.modified,
        }
        for e in idx.get_entries()
    ]
    return {"ok": True, "pages": pages}


@router.get("/api/kb/wiki/stats")
async def kb_wiki_stats() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    entries = idx.get_entries()
    by_dir: dict[str, int] = {}
    for e in entries:
        top = e.relative_path.split("/", 1)[0] if "/" in e.relative_path else "(root)"
        by_dir[top] = by_dir.get(top, 0) + 1
    tag_counts: dict[str, int] = {}
    for e in entries:
        for t in e.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    unique_tags = sorted(tag_counts, key=lambda t: (-tag_counts[t], t))[:30]
    return {
        "ok": True,
        "vault_path": cfg.vault_path,
        "total_pages": len(entries),
        "pages_by_dir": by_dir,
        "total_links": sum(len(e.links) for e in entries),
        "total_tags": len(tag_counts),
        "unique_tags": unique_tags,
        "last_indexed": idx.last_full_scan,
    }


def _vault_resolve(cfg, rel: str):
    """Resolve a vault-relative (or absolute) path and ensure it stays inside the
    vault. Returns the resolved Path or None when it escapes the vault."""
    vault = Path(cfg.vault_path).expanduser().resolve()
    p = Path(rel)
    p = (vault / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        return None
    return p


@router.get("/api/kb/wiki/page")
def kb_wiki_page(path: str = "", title: str = "") -> dict:
    """Read one vault note in full (raw text incl. frontmatter) for the preview /
    editor. Resolves by ``path`` (vault-relative) or, failing that, by ``title``
    via the index. A note that doesn't exist yet returns ``exists:false`` so the
    UI can offer to create it (Obsidian-style click-on-dangling-wikilink)."""
    from ..knowledge import frontmatter as _fm

    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    vault = Path(cfg.vault_path).expanduser().resolve()
    p = _vault_resolve(cfg, path) if path else None
    if p is None or not p.exists():
        # fall back to title → indexed page
        if title:
            ent = _kb_indexer(cfg).find_by_title(title)
            if ent:
                p = Path(ent.path)
    if p is None or not p.exists():
        # dangling link: surface a create-able stub
        stub_title = title or (Path(path).stem if path else "")
        return {
            "ok": True,
            "exists": False,
            "path": path,
            "title": stub_title,
            "tags": [],
            "links": [],
            "raw": "",
        }
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    meta, body = _fm.split_frontmatter(raw)
    rel = p.resolve().relative_to(vault).as_posix()
    return {
        "ok": True,
        "exists": True,
        "path": rel,
        "title": (meta.get("title") or "").strip() or _fm.extract_title(body) or p.stem,
        "tags": _fm._as_list(meta.get("tags")),
        "links": _fm.extract_wikilinks(body),
        "raw": raw,
    }


@router.put("/api/kb/wiki/page")
def kb_wiki_page_put(data: dict) -> dict:
    """Write a note's full raw text back to the vault (path must stay in-vault and
    end in .md), then refresh the index so the preview/list/graph update."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    rel = (data or {}).get("path", "")
    raw = (data or {}).get("raw", "")
    if not rel or not str(rel).lower().endswith((".md", ".markdown")):
        return {"ok": False, "error": "path must be a .md file"}
    p = _vault_resolve(cfg, rel)
    if p is None:
        return {"ok": False, "error": "path outside vault"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw if isinstance(raw, str) else "", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, "path": rel}


@router.post("/api/kb/wiki/page")
def kb_wiki_page_post(data: dict) -> dict:
    """Create a new note (fails if it already exists — use PUT to overwrite)."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    rel = (data or {}).get("path", "")
    raw = (data or {}).get("raw", "")
    if not rel or not str(rel).lower().endswith((".md", ".markdown")):
        return {"ok": False, "error": "path must be a .md file"}
    p = _vault_resolve(cfg, rel)
    if p is None:
        return {"ok": False, "error": "path outside vault"}
    if p.exists():
        return {"ok": False, "error": "already exists"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(raw if isinstance(raw, str) else "", encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, "path": rel}


@router.post("/api/kb/wiki/index")
def kb_wiki_index() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    n = idx.scan()
    _maybe_build_semantic(cfg)
    return {"ok": True, "indexed": n, "tags": idx.get_all_tags()}


def _kb_refresh(cfg) -> None:
    """Force the shared indexer to rescan and drop the cached association graph."""
    _kb_indexer(cfg).scan()
    _reset_kb_engines()


def _maybe_build_semantic(cfg) -> None:
    """Encode wiki pages into the semantic index after a build/reindex (no-op
    unless ``use_semantic`` is on). Failures are swallowed → lexical fallback."""
    if not getattr(cfg, "use_semantic", False):
        return
    try:
        _get_kb_semantic(cfg, _kb_indexer(cfg).get_entries(), build=True)
    except Exception:  # noqa: BLE001
        pass


def _compile_to_dict(res) -> dict:
    return {
        "created": res.created,
        "updated": res.updated,
        "new_links": res.new_links,
        "discovered": res.discovered,
    }


@router.post("/api/kb/wiki/ingest")
def kb_wiki_ingest(data: dict) -> dict:
    """Compile a single raw file (path absolute or relative to the vault)."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    vault = Path(cfg.vault_path).resolve()
    raw = (data or {}).get("path", "")
    p = Path(raw) if Path(raw).is_absolute() else (vault / raw).resolve()
    try:
        p.relative_to(vault)
    except ValueError:
        return {"ok": False, "error": "path outside vault"}
    if not p.is_file():
        # compile() silently no-ops on a missing path and returned ok:True with
        # empty created/updated — callers couldn't tell failure from empty.
        return {"ok": False, "error": "file not found"}
    comp = _kb_compiler.WikiCompiler(vault, cfg.wiki_dir, cfg.raw_dir)
    res = comp.compile(p)
    comp.update_index()
    _kb_refresh(cfg)
    return {"ok": True, **_compile_to_dict(res)}


@router.post("/api/kb/wiki/build")
def kb_wiki_build() -> dict:
    """Compile every raw file in the vault (raw→wiki) and rebuild the index."""
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    comp = _kb_compiler.WikiCompiler(Path(cfg.vault_path), cfg.wiki_dir, cfg.raw_dir)
    result = comp.build_all()
    _kb_refresh(cfg)
    _maybe_build_semantic(cfg)
    return {"ok": True, **result}


@router.get("/api/kb/wiki/related")
def kb_wiki_related(title: str = "", top_k: int = 10) -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"related": [], "clusters": []})
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).find_related(title, top_k=top_k)}


@router.get("/api/kb/wiki/discover")
def kb_wiki_discover() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured()
    idx = _kb_indexer(cfg)
    return {"ok": True, **_get_kb_engine(idx).discover()}


@router.get("/api/kb/wiki/orphans")
def kb_wiki_orphans() -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"pages": []})
    idx = _kb_indexer(cfg)
    pages = [{"title": e.title, "path": e.relative_path, "tags": e.tags} for e in idx.get_orphans()]
    return {"ok": True, "pages": pages}


@router.get("/api/kb/wiki/backlinks")
def kb_wiki_backlinks(title: str = "") -> dict:
    cfg = _load_kb_cfg()
    if not cfg.usable:
        return _kb_not_configured({"backlinks": []})
    idx = _kb_indexer(cfg)
    bl = idx.get_backlinks(title)
    return {"ok": True, "title": title, "backlinks": bl, "count": len(bl)}


@router.put("/api/kb/wiki/config")
async def kb_wiki_put_config(data: dict) -> dict:
    from dataclasses import fields as _fields

    from ..knowledge.config import save_knowledge_config as _save_kb_cfg
    from ..knowledge.types import KnowledgeConfig as _KC

    current = _load_kb_cfg()
    known = {f.name for f in _fields(_KC)}
    merged = {**current.__dict__, **{k: v for k, v in data.items() if k in known}}
    cfg = _KC(**merged)
    _save_kb_cfg(cfg)
    from ..knowledge.indexer import reset_indexers as _reset_kb

    _reset_kb()  # pick up a changed vault_path on the next call
    _reset_kb_semantic()  # embeddings are keyed by vault path
    return {"ok": True, "config": merged}
