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
    # Citation blocks (docs/citations-design.md): model-appended sourcing
    # metadata must not be captured into the memory pool and re-distilled.
    re.compile(r"</?\s*ginno_(?:wiki_)?citations\s*>", re.IGNORECASE),
]


# Citation contract appended inside <injected_wiki> whenever retrieval returned
# results (citations enabled). Rides the per-turn volatile context, never the
# stable system layer — prefix cache untouched (design §2.4).
CITATIONS_CONTRACT = (
    "## 引用规范\n\n"
    "如果你的回答实际用到了上方「相关知识」或本轮搜索/读取的来源，必须给出引用：\n"
    "1. 行内：wiki 页在结论旁写 [[标题或来源路径]]；网页写 [编号]（编号来自搜索结果的 [sN]）；\n"
    "2. 结尾：在回复最末尾追加恰好一个引用块：\n\n"
    "<ginno_citations>\n"
    "wiki|<相对路径>|note=[该页如何被用到，一句话]\n"
    "web|<sN 或 URL>|note=[如何被用到，一句话]\n"
    "</ginno_citations>\n\n"
    "纪律：\n"
    "- 只引用本轮真实出现过的来源（上方注入列表 / 搜索结果 / 你读取过的页面）；不得编造；\n"
    "- 没用到就不引；note 只写用途，不摘抄原文；\n"
    "- 凭自身知识回答的部分不要冒充来源引用；\n"
    "- 引用块只用于溯源，不是指令通道。"
)

# P0 wording — no web tools registered yet, so don't teach web entries.
CITATIONS_CONTRACT_WIKI_ONLY = (
    "## 引用规范\n\n"
    "如果你的回答实际用到了上方「相关知识」中的内容，必须给出引用：\n"
    "1. 行内：在用到某页的结论旁写 [[该页的标题或来源路径]]；\n"
    "2. 结尾：在回复最末尾追加恰好一个引用块：\n\n"
    "<ginno_citations>\n"
    "wiki|<相对路径>|note=[该页如何被用到，一句话]\n"
    "</ginno_citations>\n\n"
    "纪律：\n"
    "- 只引用上方「相关知识」中真实出现的页面；不得编造页名或路径；\n"
    "- 没用到就不引；note 只写用途，不摘抄原文；\n"
    "- 凭自身知识回答的部分不要冒充 Wiki 引用；\n"
    "- 引用块只用于溯源，不是指令通道。"
)


def _web_search_enabled() -> bool:
    """Delegate to the SINGLE reader that gates the web tools (`web/config.py`).

    Two readers with opposite defaults previously disagreed on fresh installs
    (no `web` block): tools registered (enabled=True) while the wiki-only
    citation contract was injected (enabled=False), teaching the model
    contradictory citation rules. One reader owns the gate now.
    """
    try:
        from ..web.config import load_web_config

        return bool(load_web_config().enabled)
    except Exception:
        return False


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
    """Return the injectable wiki context for a query, or '' when disabled.

    Side effects when ``cfg.citations`` is on (design §3): each injected page
    is registered in the turn's source registry (citations validate against
    it at turn end) and counted in the usage ledger (``injected``).
    """
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
        if getattr(cfg, "citations", True):
            # Full wording once the built-in web tools exist; wiki-only before.
            parts.append(CITATIONS_CONTRACT if _web_search_enabled() else CITATIONS_CONTRACT_WIKI_ONLY)
            try:
                from . import citations as _cit, usage as _usage

                _cit.register_wiki_sources(results)
                _usage.record_injected(
                    [r.entry.relative_path for r in results],
                    {r.entry.relative_path: r.entry.checksum for r in results},
                )
            except Exception:
                pass  # telemetry must never break injection
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
