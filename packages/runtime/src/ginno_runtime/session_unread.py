"""Server-side session unread (inbox semantics) — the single source of truth
for the sidebar unread dot and the Dock badge's session component.

Mirrors the teams done-unread pattern (teams/status.py §5.3.1): a session is
unread ⟺ ``last_finished > seen``. ``last_finished`` is written at the two
explicit terminal branches of a turn (completed / error — never the finally
leg, which auto-retry and interrupt parking also traverse); ``seen`` is
reported by the frontend when the user is actually watching the session.
Both fields live in the session meta (``sessions/_index.json``), so they
survive restarts and are cleaned up with the session for free. Sessions with
neither field (pre-existing) are simply not unread — no backfill needed.
"""

from __future__ import annotations

import time

from . import paths
from .session_meta import _session_meta_list, _session_meta_patch

# Dock badge / sidebar dot count these only:
# - "scheduled": shadow sessions — their unread lives in the schedule seen
#   ledger (schedule_store), counting here would double-count.
# - "team" / "team-member": team sessions bubble through /api/teams/badge.
# - "subagent" / "delegation": their "finished" semantics differ from the
#   per-turn settle (v1 exclusion; reopening is a matter of writing the
#   terminal hooks, the helpers below are type-agnostic).
EXCLUDED_TYPES = frozenset(
    {"scheduled", "team", "team-member", "subagent", "delegation"}
)


def note_turn_finished(slug: str, session_id: str, at: float | None = None) -> None:
    """Turn reached an explicit terminal state (completed / error)."""
    _session_meta_patch(slug, session_id, {"last_finished": at or time.time()})


def mark_session_seen(slug: str, session_id: str, at: float | None = None) -> None:
    """User actually looked at the session — touch=False: reading must not
    reorder the sidebar (sorted by ``updated``)."""
    _session_meta_patch(
        slug, session_id, {"seen": at or time.time()}, touch=False
    )


def is_unread(meta: dict) -> bool:
    if meta.get("type") in EXCLUDED_TYPES:
        return False
    last_finished = meta.get("last_finished")
    if not last_finished:
        return False
    return float(last_finished) > float(meta.get("seen") or 0)


def _iter_metas(slug: str | None):
    if slug is not None:
        yield from _session_meta_list(slug)
        return
    for idx in paths.home().glob("projects/*/sessions/_index.json"):
        try:
            yield from _session_meta_list(idx.parent.parent.name)
        except Exception:  # noqa: BLE001 — corrupt index must not kill the badge
            continue


def unread_count(slug: str | None = None) -> int:
    return sum(1 for m in _iter_metas(slug) if is_unread(m))
