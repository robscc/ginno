"""Search engine registry (citations-design.md §4.3).

Every engine implements ``fn(query, engine_cfg, timeout_s) -> list[SearchHit]``
using stdlib networking only (no new runtime deps). DuckDuckGo ships as the
key-free default; SearXNG (self-hosted) and Tavily (API key) cover the
configurable tier. Failures raise ``EngineError`` with an actionable message —
the tool layer converts it to the builtin "[error] …" contract.
"""

from __future__ import annotations

import html as _html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class EngineError(RuntimeError):
    pass


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""


def _http_get(url: str, timeout_s: float, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return resp.read(2_000_000)  # cap response size


def _http_post_json(url: str, payload: dict, timeout_s: float, headers: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "User-Agent": _UA,
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return json.loads(resp.read(2_000_000).decode("utf-8", "replace"))


# ----------------------------- engines ----------------------------- #


def _ddg(query: str, cfg: dict, timeout_s: float) -> list[SearchHit]:
    """DuckDuckGo HTML endpoint (key-free). Regex-parse the lite results."""
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    body = _http_get(url, timeout_s).decode("utf-8", "replace")
    hits: list[SearchHit] = []
    # result blocks: <a rel="nofollow" class="result__a" href="...">Title</a>
    # followed (later, same block) by <a class="result__snippet"...>snippet</a>
    blocks = re.split(r'class="result__a"', body)[1:]
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.DOTALL)
    for i, blk in enumerate(blocks):
        href_m = re.match(r'\s*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.DOTALL)
        if not href_m:
            continue
        href, title = href_m.group(1), _strip_tags(href_m.group(2))
        target = _ddg_unwrap(href)
        if not target:
            continue
        hits.append(
            SearchHit(
                title=_html.unescape(title).strip(),
                url=target,
                snippet=_html.unescape(_strip_tags(snippets[i])).strip() if i < len(snippets) else "",
            )
        )
    if not hits and body and "result__a" not in body:
        raise EngineError("DuckDuckGo 返回了无法解析的页面（可能被限流），稍后重试或配置其它引擎")
    return hits


def _ddg_unwrap(href: str) -> str:
    """DDG wraps results in //duckduckgo.com/l/?uddg=<urlencoded>; unwrap."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urllib.parse.urlsplit(href)
    except ValueError:
        return ""
    if "duckduckgo.com" in (parts.netloc or "") and parts.path.startswith("/l/"):
        qs = urllib.parse.parse_qs(parts.query)
        uddg = (qs.get("uddg") or [""])[0]
        return uddg if uddg.startswith("http") else ""
    return href if href.startswith(("http://", "https://")) else ""


def _searxng(query: str, cfg: dict, timeout_s: float) -> list[SearchHit]:
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise EngineError("searxng 未配置 base_url（settings.web.engines.searxng.base_url）")
    url = base + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    data = json.loads(_http_get(url, timeout_s, {"Accept": "application/json"}).decode("utf-8", "replace"))
    return [
        SearchHit(
            title=_html.unescape(str(r.get("title") or "")).strip(),
            url=str(r.get("url") or ""),
            snippet=_html.unescape(str(r.get("content") or "")).strip(),
        )
        for r in (data.get("results") or [])
        if r.get("url")
    ]


def _tavily(query: str, cfg: dict, timeout_s: float) -> list[SearchHit]:
    key = cfg.get("api_key") or ""
    if not key:
        raise EngineError("tavily 未配置 api_key（settings.web.engines.tavily.api_key）")
    data = _http_post_json(
        "https://api.tavily.com/search",
        {"api_key": key, "query": query, "max_results": 10, "include_answer": False},
        timeout_s,
    )
    return [
        SearchHit(
            title=_html.unescape(str(r.get("title") or "")).strip(),
            url=str(r.get("url") or ""),
            snippet=_html.unescape(str(r.get("content") or "")).strip(),
        )
        for r in (data.get("results") or [])
        if r.get("url")
    ]


def _strip_tags(fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", fragment).strip()


ENGINES: dict[str, Callable[[str, dict, float], list[SearchHit]]] = {
    "duckduckgo": _ddg,
    "searxng": _searxng,
    "tavily": _tavily,
}

ENGINE_NAMES = tuple(ENGINES)


def search(query: str, engine: str, cfg: dict, timeout_s: float, max_results: int) -> list[SearchHit]:
    fn = ENGINES.get(engine)
    if fn is None:
        raise EngineError(f"未知搜索引擎 {engine!r}，可用: {', '.join(ENGINES)}")
    hits = fn(query, cfg, timeout_s)
    out = [h for h in hits if h.url.startswith(("http://", "https://"))]
    return out[: max(1, min(max_results, 10))]
