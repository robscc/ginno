"""Built-in web search & fetch (docs/citations-design.md §4).

Engine-pluggable search (DuckDuckGo default — no key needed; SearXNG/Tavily
configurable) plus a safety-guarded page fetcher. Both feed the turn source
registry so answers can cite web sources with stable ids.
"""

from .config import WebConfig, load_web_config
from .engines import ENGINE_NAMES, SearchHit, search
from .fetch import fetch_page

__all__ = [
    "WebConfig",
    "load_web_config",
    "ENGINE_NAMES",
    "SearchHit",
    "search",
    "fetch_page",
]
