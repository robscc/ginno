"""Memory status: what MEMORY.md holds and what actually rides the prompt.

The Settings → 智能体 → 记忆 page shows "已生效的记忆" — not just the file on
disk, but the text the model receives this turn: the global ``MEMORY.md`` plus
the current agent's private memory, wrapped and capped by
``context.memory_max_chars`` (``world_state.memory``). Keeping this projection
in one place means the page and the prompt can never disagree.
"""

from __future__ import annotations

import time
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths


def _sections(text: str) -> list[dict[str, Any]]:
    """Split MEMORY.md on ``## `` headings; text before the first one is a group."""
    out: list[dict[str, Any]] = []
    heading = ""
    lines: list[str] = []

    def flush() -> None:
        if not any(line.strip() for line in lines):
            return
        body = "\n".join(lines)
        bullets = [ln for ln in lines if ln.strip().startswith(("-", "*", "·"))]
        out.append(
            {
                "heading": heading,
                "title": heading.lstrip("# ").strip() or "(未分组)",
                "chars": len(body) + (len(heading) + 1 if heading else 0),
                "lines": len(bullets),
                "text": body,
            }
        )

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            heading = line
            lines = []
        else:
            lines.append(line)
    flush()
    return out


def _agent_ids() -> list[dict[str, str]]:
    try:
        from .. import agents as agents_reg

        return [
            {"id": a.id, "name": a.name}
            for a in agents_reg.list_agents()
            if getattr(a, "id", None)
        ]
    except Exception:  # noqa: BLE001 — registry broken: show global memory only
        return []


def _agent_memory(agent_id: str) -> tuple[str, int, int]:
    """(text, chars, entry_count) for one agent's private memory."""
    base = paths.agents_dir() / agent_id
    idx = base / "MEMORY.md"
    text = ""
    if idx.exists():
        try:
            text = idx.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
    entries = 0
    md = base / "memory"
    if md.exists():
        try:
            entries = len([p for p in md.glob("*.md") if p.is_file()])
        except OSError:
            entries = 0
    return text, len(text), entries


def render_injected(global_text: str, agent_text: str, budget: int) -> tuple[str, bool]:
    """Mirror ``world_state.memory.render`` — the exact text sent to the model.

    Returns ``(text, truncated)``. Kept in sync with the renderer by shape, not
    by import: the renderer takes a snapshot dict and lives in the prompt stack;
    this projection must stay cheap enough for an HTTP endpoint.
    """
    from ..knowledge.injection import wrap_context_section
    from ..lang import t

    parts: list[str] = []
    if agent_text:
        parts.append("Your persistent memory (private to this agent):\n" + agent_text)
    if global_text:
        parts.append(wrap_context_section("injected_memory", global_text))
    out = "\n".join(p for p in parts if p)
    truncated = False
    if budget > 0 and len(out) > budget:
        out = out[:budget] + t(
            "\n…(memory content exceeded the injection budget and was truncated; "
            "for the full text read MEMORY.md and the persona memory files under "
            "ginno_home with file tools)",
            "\n…（记忆内容超出注入预算已截断，如需完整内容请用文件工具读取 "
            "ginno_home 下的 MEMORY.md 与角色记忆文件）",
        )
        truncated = True
    return out, truncated


def memory_status(agent_id: str | None = None) -> dict[str, Any]:
    """File view + prompt view of the memory that is currently in effect."""
    from ..world_state import DEFAULT_MEMORY_MAX_CHARS, context_settings

    p = paths.memory_index_path()
    global_text = ""
    updated_at: float | None = None
    if p.exists():
        try:
            global_text = p.read_text(encoding="utf-8")
            updated_at = p.stat().st_mtime
        except OSError:
            pass

    agents = _agent_ids()
    chosen = agent_id or (agents[0]["id"] if agents else "")
    agent_text, agent_chars, agent_entries = _agent_memory(chosen) if chosen else ("", 0, 0)

    budget = int(context_settings().get("memory_max_chars", DEFAULT_MEMORY_MAX_CHARS) or 0)
    injected, truncated = render_injected(global_text, agent_text, budget)

    agent_rows = []
    for a in agents:
        text, chars, entries = _agent_memory(a["id"])
        agent_rows.append(
            {
                "id": a["id"],
                "name": a["name"],
                "chars": chars,
                "entries": entries,
                "selected": a["id"] == chosen,
                "has_index": bool(text),
            }
        )

    sections = _sections(global_text)
    return {
        "path": str(p),
        "exists": bool(global_text),
        "updated_at": updated_at,
        "chars": len(global_text),
        "budget": budget,
        "sections": sections,
        "section_count": len(sections),
        "line_count": sum(s["lines"] for s in sections),
        "agent_id": chosen,
        "agent_chars": agent_chars,
        "agent_entries": agent_entries,
        "agents": agent_rows,
        "injected_chars": len(injected),
        "injected_preview": injected[:2000],
        "truncated": truncated,
    }


def touch_updated_at() -> float:
    """Last-write timestamp of MEMORY.md (0 when absent)."""
    p = paths.memory_index_path()
    try:
        return p.stat().st_mtime if p.exists() else 0.0
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Daily snapshots (pool watermark / memory budget trend)
# ---------------------------------------------------------------------------

def _state_path():
    return paths.memory_dir() / "state.jsonl"


def record_snapshot() -> dict | None:
    """Write today's watermark snapshot, replacing an earlier one for the day.

    One line per local day keeps ``state.jsonl`` ~365 lines a year; the sweep
    runs every 10 minutes so the day's line tracks the latest values rather
    than freezing whatever startup saw.
    """
    from .pool import pool_count

    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d", time.localtime())
        now = time.time()
        idx = paths.memory_index_path()
        memory_chars = len(idx.read_text(encoding="utf-8")) if idx.exists() else 0
        from ..world_state import DEFAULT_MEMORY_MAX_CHARS, context_settings

        budget = int(context_settings().get("memory_max_chars", DEFAULT_MEMORY_MAX_CHARS) or 0)
        from .summarize import pending_drafts

        row = {
            "ts": now,
            "date": today,
            "pool": pool_count(),
            "memory_chars": memory_chars,
            "budget": budget,
            "drafts_pending": len(pending_drafts()),
        }
        kept: list[str] = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    day = str(json.loads(line).get("date") or "")
                except ValueError:
                    continue
                if day and day != today:
                    kept.append(line)
        kept.append(json.dumps(row, ensure_ascii=False))
        p.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return row
    except Exception:  # noqa: BLE001 — a snapshot must never break the sweep
        return None


def memory_trend(days: int = 30) -> list[dict[str, Any]]:
    """Oldest-first daily snapshots for the trailing window."""
    p = _state_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("date"):
            rows.append(row)
    rows.sort(key=lambda r: str(r.get("date")))
    return rows[-max(days, 1):]
