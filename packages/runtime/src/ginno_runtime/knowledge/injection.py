"""Build the wiki context block injected into the agent system prompt.

`build_wiki_context(query)` retrieves the most relevant vault entries for the
user's current message and formats them (plus directory guidelines) for
inclusion in the prompt, wrapped by the caller in an `<injected_wiki>` section.

Includes light sanitization helpers (reused by the P2 memory refinery) that
strip instruction-like injection patterns so retrieved/summarized content is
treated as data, not commands.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import paths as _paths
from .config import load_knowledge_config
from .indexer import get_indexer
from .retriever import WikiRetriever
from .semantic import get_semantic_index
from .types import KnowledgeConfig, RetrievalResult

# instruction-like wrappers an attacker could smuggle into vault/memory content.
# This is the CANONICAL list — memory/pool.py imports it so the two sanitize
# paths (retrieval injection and memory capture) can never drift apart.
_INJECTION_PATTERNS = [
    re.compile(r"</?\s*injected_(wiki|memory)\s*>", re.IGNORECASE),
    re.compile(r"</?\s*system_prompt\s*>", re.IGNORECASE),
    re.compile(r"</?\s*instruction_hierarchy\s*>", re.IGNORECASE),
    # Prompt sections built by the command/mention resolver and the file
    # attachment path (<mentioned_workflow>, <skill name="x">, <attached_files>).
    re.compile(r"</?\s*(?:mentioned_\w+|attached_files|skill)\b[^>]*>", re.IGNORECASE),
]


def wrap_context_section(name: str, content: str) -> str:
    return f"<{name}>\n{content}\n</{name}>"


def sanitize_for_memory(text: str) -> str:
    """Strip injection wrappers before text is stored (capture) or summarized."""
    out = text
    for rx in _INJECTION_PATTERNS:
        out = rx.sub("", out)
    return out.strip()


# alias kept for the P2 refinery naming parity with Molly
sanitize_memory_output = sanitize_for_memory


def get_wiki_guidelines(cfg: KnowledgeConfig) -> str:
    # Present ABSOLUTE vault paths so the agent writes into the real vault even
    # when the session workspace differs. Relative dirs previously made the agent
    # emit paths like "/Ginno/Raw/…" (base lost) and crash write_file.
    vault = Path(str(cfg.vault_path)).expanduser()
    raw = vault / cfg.raw_dir
    research = vault / cfg.research_dir
    wiki = vault / cfg.wiki_dir
    return (
        "## Obsidian Wiki 使用规范\n\n"
        "向 Obsidian vault 写入新文档时，请遵循以下目录结构（**绝对路径**）：\n\n"
        "| 目录 | 用途 | 是否可写 |\n"
        "|------|------|----------|\n"
        f"| `{raw}/` | 原始文档、笔记、报告 | ✅ 新文档写这里 |\n"
        f"| `{research}/` | 深度研究报告 | ✅ 研究报告写这里 |\n"
        f"| `{wiki}/` | 自动编译的 wiki 页（KB 页 “Build wiki” / POST /kb/wiki/build 产物） | ❌ 勿直接写入 |\n\n"
        f"规则：新文档/报告/总结一律存到 `{raw}/`；"
        f"`{wiki}/` 由 KB 页 “Build wiki”（POST /kb/wiki/build）从 Raw/ 自动生成，不要手写（没有 /kb build 命令）。"
    )


def format_wiki_context(results: list[RetrievalResult]) -> str:
    sections = ["## 相关知识 (来自 Obsidian Wiki)\n"]
    for r in results:
        e = r.entry
        tags = ", ".join(e.tags) if e.tags else ""
        header = f"### {e.title}" + (f" ({tags})" if tags else "")
        meta = f"来源: [[{e.relative_path}]] | 相关度: {int(round(r.score * 100))}%"
        if r.matched_terms:
            shown = r.matched_terms[:8]
            extra = len(r.matched_terms) - len(shown)
            terms = " · ".join(shown) + (f" · +{extra}" if extra > 0 else "")
            meta += "\n命中: " + terms
        block = f"{header}\n{meta}\n\n{r.snippet}".rstrip()
        sections.append(block)
    return "\n\n---\n\n".join(sections)


def build_wiki_context(query: str, cfg: KnowledgeConfig | None = None) -> str:
    """Return the injectable wiki context for a query, or '' when disabled."""
    cfg = cfg or load_knowledge_config()
    if not cfg.usable or not query.strip():
        return ""
    parts = [get_wiki_guidelines(cfg)]
    try:
        # Index the whole vault minus the raw sources dir, so finished notes
        # anywhere (e.g. 股市/) are injectable, not just compiled wiki pages.
        idx = get_indexer(
            cfg.vault_path,
            cfg.rescan_interval_s,
            exclude_dirs=[cfg.raw_dir] if cfg.raw_dir else None,
        )
        entries = idx.get_entries()
        # build=False: never block injection on a full re-encode; if no cached
        # semantic index exists yet, sem is None and retrieval stays lexical.
        sem = get_semantic_index(cfg, entries)
        results = WikiRetriever(entries).retrieve(
            query,
            top_k=cfg.inject_top_k,
            min_score=cfg.inject_min_score,
            semantic=sem,
            semantic_weight=cfg.semantic_weight,
        )
    except Exception:
        results = []
    if results:
        parts.append(format_wiki_context(results))
    return "\n\n".join(parts)


def read_global_memory() -> str:
    """Return the distilled global memory (~/.ginno/MEMORY.md) text, if any.

    Used by the P2 memory refinery for injection. Returns '' for the default
    boilerplate so an un-summarized index is not injected as noise.
    """
    p = _paths.memory_index_path()
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    if not text or text.startswith("# Ginno Memory"):
        return ""
    return text
