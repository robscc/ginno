"""Deterministic KB wiki entries for the「Wiki 编译」workflow.

The wiki pages themselves are written by AGENT steps (agent-wiki-workflow-
design.md) — these entries only do the deterministic scaffolding around them:

* ``kb_wiki_inventory`` — diff ``Raw/`` against the compile ledger
  (``~/.ginno/knowledge/wiki-inventory.json``) → the list of docs needing
  compilation (new / changed / page deleted). Replaces the old build_all's
  ledger skip: the LLM work is fan-out agent turns, this stays free.
* ``kb_wiki_finalize`` — absorb the compile step's WRITE_JSON
  (``compiled``: doc/page pairs) into the ledger, regenerate ``INDEX.md``
  and lint broken ``[[links]]``.
* ``kb_wiki_lint`` — standalone broken-link report.

All read config single-sourced from ``load_knowledge_config()`` — the DSL
carries no paths, so re-pointing the vault is a Settings change, not a DSL
edit. Deterministic, no LLM (python-node contract).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from ... import paths
from ...knowledge import frontmatter as fm
from ...knowledge import maintenance
from ...knowledge.config import load_knowledge_config

_SKIP_DIRS = {"node_modules", ".obsidian", ".trash", ".git", ".vscode", ".ginno", ".molly"}
_MD_EXTS = {".md", ".markdown"}

_SLUG_RE = re.compile(r'[<>:"/\\|?*%\x00-\x1f]')


def _slug(name: str) -> str:
    """Page-file slug — MUST match the compile-step goal's naming rule
    (``Wiki/<slug>.md``), or the inventory's page-existence check misses."""
    s = _SLUG_RE.sub("", name or "")
    s = re.sub(r"-+", "-", s.strip().replace(" ", "-")).strip("-").lower()
    return (s or "untitled")[:80]


def _load_ledger() -> dict:
    try:
        return json.loads(
            (paths.knowledge_dir() / "wiki-inventory.json").read_text(encoding="utf-8") or "{}"
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_ledger(ledger: dict) -> None:
    try:
        p = paths.knowledge_dir() / "wiki-inventory.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 台账写失败只影响下次增量,不阻塞本次运行


def _doc_title(p: Path) -> str:
    try:
        meta, body = fm.split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return p.stem
    return ((meta.get("title") or "").strip() or fm.extract_title(body) or p.stem).strip()


def _iter_raw_md(raw_root: Path):
    for dirpath, dirnames, filenames in os.walk(raw_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            f = Path(dirpath) / fn
            if f.suffix.lower() in _MD_EXTS and f.name != "INDEX.md":
                yield f


def _cfg_vault():
    cfg = load_knowledge_config()
    return cfg, Path(cfg.vault_path).expanduser().resolve()


def kb_wiki_inventory(args: dict) -> dict:
    """Diff Raw/ against the ledger → ``{files: [...], raw_total}``."""
    cfg, vault = _cfg_vault()
    if not cfg.usable or not vault.is_dir():
        return {"files": [], "raw_total": 0, "error": "knowledge not configured"}
    ledger = _load_ledger()
    files: list[dict] = []
    total = 0
    for f in _iter_raw_md(vault / cfg.raw_dir):
        total += 1
        rel = f.resolve().relative_to(vault).as_posix()
        try:
            chk = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            continue
        e = ledger.get(rel)
        if e and e.get("checksum") == chk:
            title = str(e.get("title") or "")
            if title and (vault / cfg.wiki_dir / f"{_slug(title)}.md").exists():
                continue  # 已编译且产物页仍在
            files.append({"path": str(f), "rel": rel, "title": title or _doc_title(f), "reason": "missing_page"})
        else:
            files.append(
                {
                    "path": str(f),
                    "rel": rel,
                    "title": _doc_title(f),
                    "reason": "new" if not e else "changed",
                }
            )
    return {
        "files": files,
        "raw_total": total,
        "wiki_dir": cfg.wiki_dir,
        "raw_dir": cfg.raw_dir,
        "vault": str(vault),
    }


def kb_wiki_finalize(args: dict) -> dict:
    """Absorb the compile step's ``compiled`` (doc/page pairs): mark the
    ledger, regenerate INDEX.md, lint broken links → ``report``."""
    cfg, vault = _cfg_vault()
    if not cfg.usable:
        return {"marked": [], "indexed": 0, "broken": [], "error": "knowledge not configured"}
    ledger = _load_ledger()
    marked: list[str] = []
    for c in args.get("compiled") or []:
        if not isinstance(c, dict):
            continue
        rel = str(c.get("doc") or "").strip()
        if not rel:
            continue
        f = vault / rel
        if not f.is_file():
            continue
        try:
            chk = hashlib.sha256(f.read_bytes()).hexdigest()
        except OSError:
            continue
        ledger[rel] = {
            "checksum": chk,
            "title": str(c.get("page") or "").strip() or _doc_title(f),
            "ts": time.time(),
        }
        marked.append(rel)
    _save_ledger(ledger)
    index_text = maintenance.update_index(vault, cfg.wiki_dir)
    broken = maintenance.lint_links(vault, cfg.wiki_dir)
    return {
        "marked": marked,
        "indexed": index_text.count("- [["),
        "broken": broken,
    }


def kb_wiki_lint(args: dict) -> dict:
    """Standalone broken-``[[link]]`` report over the wiki dir."""
    cfg, vault = _cfg_vault()
    if not cfg.usable:
        return {"broken": [], "error": "knowledge not configured"}
    return {"broken": maintenance.lint_links(vault, cfg.wiki_dir)}
