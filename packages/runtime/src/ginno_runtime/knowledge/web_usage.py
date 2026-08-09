"""Web search telemetry ledger (docs/citations-design.md §4.6 / §6.2).

``~/.ginno/knowledge/web_usage.json`` — engine- and domain-level counters.
The ``hits_cited / searches`` ratio is the engine's "results actually used"
metric (shown in Settings); the domain table feeds KB Discover's
"highly-cited external domains" section and the one-click Raw/ archiving.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.parse
from typing import Any

from .. import paths

_LOCK = threading.RLock()


def web_usage_path():
    return paths.knowledge_dir() / "web_usage.json"


def _load() -> dict[str, Any]:
    p = web_usage_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    p = web_usage_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".web-usage-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _domain_of(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def record_search(engine: str, n_results: int) -> None:
    with _LOCK:
        data = _load()
        e = data.setdefault("engines", {}).setdefault(engine or "unknown", {})
        e["searches"] = int(e.get("searches") or 0) + 1
        e["results"] = int(e.get("results") or 0) + max(0, n_results)
        e["last_search"] = time.time()
        _save(data)


def record_cited(url: str, engine: str = "", fetched: bool = False) -> None:
    """A verified web citation: credit the domain (and the engine's hit rate)."""
    dom = _domain_of(url)
    if not dom:
        return
    now = time.time()
    with _LOCK:
        data = _load()
        d = data.setdefault("domains", {}).setdefault(dom, {})
        d["cited"] = int(d.get("cited") or 0) + 1
        if fetched:
            d["fetched"] = int(d.get("fetched") or 0) + 1
        d["last_cited"] = now
        if engine:
            e = data.setdefault("engines", {}).setdefault(engine, {})
            e["hits_cited"] = int(e.get("hits_cited") or 0) + 1
        _save(data)


def record_fetched(url: str) -> None:
    dom = _domain_of(url)
    if not dom:
        return
    with _LOCK:
        data = _load()
        d = data.setdefault("domains", {}).setdefault(dom, {})
        d["fetched"] = int(d.get("fetched") or 0) + 1
        d["last_fetched"] = time.time()
        _save(data)


def summary() -> dict[str, Any]:
    data = _load()
    engines = data.get("engines") or {}
    domains = data.get("domains") or {}
    eng_rows = []
    for name, e in engines.items():
        searches = int(e.get("searches") or 0)
        eng_rows.append(
            {
                "engine": name,
                "searches": searches,
                "hits_cited": int(e.get("hits_cited") or 0),
                "cite_rate": round(int(e.get("hits_cited") or 0) / searches, 3) if searches else 0.0,
                "last_search": e.get("last_search"),
            }
        )
    eng_rows.sort(key=lambda r: (-r["searches"], r["engine"]))
    dom_rows = [
        {
            "domain": dom,
            "cited": int(d.get("cited") or 0),
            "fetched": int(d.get("fetched") or 0),
            "last_cited": d.get("last_cited"),
        }
        for dom, d in domains.items()
    ]
    dom_rows.sort(key=lambda r: (-r["cited"], -r["fetched"], r["domain"]))
    return {
        "engines": eng_rows,
        "top_domains": dom_rows[:20],
        "total_searches": sum(r["searches"] for r in eng_rows),
        "total_cited": sum(r["cited"] for r in dom_rows),
    }


def reset() -> None:
    with _LOCK:
        _save({})
