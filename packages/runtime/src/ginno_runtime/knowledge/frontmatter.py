"""Markdown frontmatter parsing + body helpers for wiki entries.

Uses PyYAML (already a runtime dependency, see skills/loader.py) to parse the
frontmatter block, then derives title/summary/wikilinks from the body.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Leading `---\n … \n---\n` frontmatter block; body is whatever follows.
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)\Z", re.DOTALL)
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]+)?\]\]")
_H1_RE = re.compile(r"^#[ \t]+(.+?)\s*$", re.MULTILINE)


def split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Return (meta, body). meta is {} when there is no valid frontmatter."""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, m.group(2)


def extract_title(body: str) -> str | None:
    """First H1 heading text, if any."""
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else None


def extract_summary(body: str, max_chars: int = 200) -> str:
    """First real paragraph (skips headings, code fences, tables, quotes, rules)."""
    para: list[str] = []
    in_code = False
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            if para:
                break
            continue
        if in_code:
            continue
        if not s:
            if para:
                break
            continue
        if s.startswith(("#", "|", ">", "---")):
            if para:
                break
            continue
        para.append(s)
    text = " ".join(para).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def extract_wikilinks(body: str) -> list[str]:
    """De-duplicated [[link]] targets (without alias/heading), in order."""
    seen: dict[str, None] = {}
    for target in _WIKILINK_RE.findall(body):
        t = target.strip()
        if t:
            seen.setdefault(t, None)
    return list(seen.keys())


def _as_list(value: Any) -> list[str]:
    """Normalize a frontmatter value that may be a list or a comma string."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(value)]
