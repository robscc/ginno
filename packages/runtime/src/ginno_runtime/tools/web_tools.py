"""Built-in web search & fetch tools (docs/citations-design.md §4.2).

Session-bound: results register into the turn's source registry (the
citation contract lets the model quote ``[sN]`` ids), and engine/domain
telemetry lands in ``web_usage.json``. Builtin contract: never raise —
failures degrade to ``[error] …`` tool results.
"""

from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from ..knowledge import citations as cit
from ..knowledge import web_usage
from ..web.config import load_web_config
from ..web.engines import EngineError, search as engine_search
from ..web.fetch import FetchError, fetch_page

WEB_TOOL_NAMES = ("web_search", "web_fetch")

_SNIPPET_CAP = 240


def build_web_tools(session_id: str | None = None) -> list:
    """The web tools, or [] when search is disabled in settings.

    Called per session from ``build_all_tools``; ``session_id`` binds source
    registration (callers without a session — e.g. workflow listings — still
    get working tools, just without citation bookkeeping).
    """
    cfg = load_web_config()
    if not cfg.enabled:
        return []

    sid = session_id or ""

    @tool
    def web_search(query: str, max_results: Optional[int] = None, engine: Optional[str] = None) -> str:
        """Search the web. Returns numbered results — cite them in your final
        answer as [sN] per the citation rules. Use for up-to-date facts,
        documentation, or anything beyond your knowledge and the user's wiki.

        Args:
            query: Search query (be specific; include key terms).
            max_results: Optional cap (default from settings, max 10).
            engine: Optional engine override (e.g. duckduckgo/searxng/tavily).
        """
        name = (engine or "").strip() or cfg.default_engine
        n = max_results or cfg.max_results
        try:
            hits = engine_search(query, name, cfg.engine_cfg(name), cfg.timeout_s, n)
        except (EngineError, Exception) as e:  # noqa: BLE001 — builtin contract
            if isinstance(e, EngineError):
                return f"[error] 网络搜索失败: {e}"
            return f"[error] 网络搜索失败: {type(e).__name__}: {e}（可稍后重试或换引擎）"
        # Count EVERY completed search (including zero-hit ones) — the engine
        # cite_rate (hits_cited/searches) is misleading if empty results don't
        # enter the denominator.
        try:
            web_usage.record_search(name, len(hits))
        except Exception:
            pass
        if not hits:
            return f"[search] 引擎 {name} 没有找到与 {query!r} 相关的结果。"
        lines = [f"[search:{name}] {query!r} — {len(hits)} 条结果：", ""]
        for h in hits:
            src = cit.register_source_for(
                sid,
                {
                    "kind": "web",
                    "identity": h.url,
                    "title": h.title or h.url,
                    "origin": "search",
                    "depth": "snippet",
                    "engine": name,
                    "query": query,
                },
            )
            mark = f"[{src['id']}]" if src else "[·]"
            snippet = (h.snippet or "").strip().replace("\n", " ")
            if len(snippet) > _SNIPPET_CAP:
                snippet = snippet[:_SNIPPET_CAP] + "…"
            lines.append(f"{mark} {h.title or '(无标题)'} — {_host_of(h.url)}")
            lines.append(f"    {h.url}")
            if snippet:
                lines.append(f"    摘要: {snippet}")
            lines.append("")
        return "\n".join(lines)

    @tool
    def web_fetch(url: str) -> str:
        """Fetch one URL and return its readable text (title + body, capped).
        Use after web_search to read a promising result in full — a cited
        source you actually read is stronger grounding than a search snippet.

        Args:
            url: Absolute http(s) URL (public hosts only).
        """
        try:
            page = fetch_page(url, timeout_s=cfg.timeout_s)
        except (FetchError, Exception) as e:  # noqa: BLE001 — builtin contract
            if isinstance(e, FetchError):
                return f"[error] 抓取失败: {e}"
            return f"[error] 抓取失败: {type(e).__name__}: {e}"
        src = cit.upgrade_web_source(sid, page["final_url"] or url, title=page["title"])
        mark = f"[{src['id']}]" if src else "[·]"
        try:
            web_usage.record_fetched(page["final_url"] or url)
        except Exception:
            pass
        truncated = "\n[内容过长已截断]" if page.get("truncated") else ""
        title = page.get("title") or "(无标题)"
        return f"{mark} 已读取原文: {title}\nURL: {page['final_url'] or url}\n\n{page['text']}{truncated}"

    return [web_search, web_fetch]


def _host_of(url: str) -> str:
    try:
        import urllib.parse

        return (urllib.parse.urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
