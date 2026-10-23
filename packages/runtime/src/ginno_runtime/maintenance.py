"""Startup data hygiene — the ``app-iter-harness`` ticket batch of 2026-10-23.

Probe ``04_ginno_ux`` surfaced long-lived dirty state in ``~/.ginno``: orphan
artifacts, sessions whose workspace vanished, legacy sessions with no agent,
stale error badges, duplicate automatic titles and title jobs that never got a
second chance after a restart. None of it is produced by a bug in a single
turn — it accumulates because nothing ever sweeps.

Every step here is idempotent and best-effort: a corrupt index must never keep
the app from starting, and a second run after a clean pass writes nothing.
Called once from ``server.lifespan`` before requests are served (title re-queue
is awaited separately, it needs a running loop to spawn background tasks).
"""

from __future__ import annotations

import os
import time
from collections import Counter

from . import agents as agents_reg
from . import artifacts as art_store
from . import paths
from .server_shared import _log
from .session_meta import (
    _session_meta_list,
    _session_meta_patch,
    _session_meta_rewrite_all,
)

# Ticket threshold (ux_fix_tickets.md DATA-04).
STALE_ERROR_DAYS = 7


def _project_slugs() -> list[str]:
    root = paths.home() / "projects"
    try:
        return sorted(
            p.name for p in root.iterdir() if (p / "sessions" / "_index.json").exists()
        )
    except OSError:
        return []


def _default_agent_id() -> str | None:
    try:
        lst = agents_reg.list_agents()
    except Exception:  # noqa: BLE001 — agent registry broken: nothing to backfill with
        return None
    return lst[0].id if lst else None


# ---- per-project index hygiene ------------------------------------------- #


def _heal_session_metas(slug: str) -> None:
    metas = _session_meta_list(slug)
    if not metas:
        return
    changed = False
    now = time.time()

    # DATA-02 — workspace deleted out from under the session (legacy /tmp/gw).
    # Fall back to the session's own files dir, which is what create_session
    # uses for every session anyway, so later turns and the code panel work.
    for m in metas:
        ws = m.get("workspace")
        if not ws or os.path.isdir(str(ws)):
            continue
        sid = m.get("id")
        if not sid:
            continue
        target = paths.session_files_dir(slug, sid)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        m["workspace"] = str(target)
        changed = True
        _log.info("maintenance_workspace_healed session=%s old=%r", sid, ws)

    # DATA-03 — pre-agent-binding sessions fall back to the first agent, which
    # is what opening them already resolves to implicitly; persisting it makes
    # the list/search/API agree.
    default_agent = _default_agent_id()
    if default_agent:
        for m in metas:
            if not str(m.get("agent_id") or "").strip():
                m["agent_id"] = default_agent
                changed = True

    # DATA-04 — error badges older than a week are archaeology, not status.
    # (A successful turn already clears last_error; this only ages out sessions
    # that were never touched again.)
    cutoff = now - STALE_ERROR_DAYS * 86400
    for m in metas:
        err = m.get("last_error")
        if not err:
            continue
        at = err.get("at") if isinstance(err, dict) else None
        if (at or m.get("updated") or 0) < cutoff:
            m["last_error"] = {}
            changed = True

    # UX-02 — automatic titles repeat on every scheduled run ("⏰ 每日总结" ×20),
    # so the sidebar has no way to tell yesterday's from today's. Suffix the
    # duplicates with their creation date (short id on same-day collisions).
    groups: dict[str, list[dict]] = {}
    for m in metas:
        title = str(m.get("title") or "").strip()
        if title:
            groups.setdefault(title, []).append(m)
    taken = {t for t, rows in groups.items() if len(rows) <= 1}
    for title, rows in groups.items():
        if len(rows) <= 1:
            continue
        for m in rows:
            created = m.get("created") or now
            day = time.strftime("%m-%d", time.localtime(created))
            candidate = f"{title} · {day}"
            if candidate in taken:
                candidate = f"{candidate} · {str(m.get('id') or '')[:4]}"
            taken.add(candidate)
            m["title"] = candidate
            changed = True

    if changed:
        _session_meta_rewrite_all(slug, metas)
        _log.info(
            "maintenance_session_index slug=%s sessions=%d", slug, len(metas)
        )


# ---- artifacts ------------------------------------------------------------ #


def _valid_session_ids() -> set[str]:
    ids: set[str] = set()
    for slug in _project_slugs():
        ids.update(str(m.get("id")) for m in _session_meta_list(slug) if m.get("id"))
    return ids


def _prune_artifacts(slug: str, valid_sessions: set[str]) -> None:
    """DATA-01 — drop entries that can only fail when clicked: the file is
    gone, or the session they belong to was deleted (their per-session upload
    dirs went with it). Panel entries only; no files are touched."""
    dropped = art_store.prune_artifacts(slug, valid_sessions)
    if dropped:
        _log.info("maintenance_artifacts_pruned slug=%s dropped=%d", slug, dropped)


# ---- title generation re-queue ------------------------------------------- #


def _first_assistant_text(slug: str, session_id: str) -> str:
    """First assistant reply in the persisted checkpoint (title seed body)."""
    try:
        from .checkpointer import FileCheckpointer
        from .title_gen import _content_text

        tup = FileCheckpointer(slug).get_tuple(
            {"configurable": {"thread_id": session_id}}
        )
        if not tup or not tup.checkpoint:
            return ""
        for m in (tup.checkpoint.get("channel_values") or {}).get("messages") or []:
            if getattr(m, "type", None) == "ai":
                text = _content_text(m).strip()
                if text:
                    return text[:2000]
    except Exception:  # noqa: BLE001 — best effort, the next turn retries anyway
        return ""
    return ""


async def requeue_pending_titles() -> int:
    """UX-03 — title jobs die with the process; a session that never gets
    another turn would keep its truncated placeholder forever. Re-arm them at
    startup, backfilling ``title_assistant`` from the stored history."""
    from .title_gen import PENDING_TITLE_GIVE_UP_HOURS, spawn_title_gen

    count = 0
    now = time.time()
    for slug in _project_slugs():
        for m in _session_meta_list(slug):
            if not m.get("title_llm_pending"):
                continue
            if now - (m.get("created") or 0) < PENDING_TITLE_GIVE_UP_HOURS * 3600:
                continue
            sid = str(m.get("id") or "")
            if not sid or not str(m.get("title_seed") or "").strip():
                continue
            assistant = str(m.get("title_assistant") or "").strip()
            if not assistant:
                assistant = _first_assistant_text(slug, sid)
                if not assistant:
                    # A day later and there is still no assistant prose to
                    # titler from (history with only tool calls, e.g. a turn
                    # interrupted before the final answer). Keep the truncated
                    # placeholder and stop flagging the session as pending.
                    _session_meta_patch(
                        slug,
                        sid,
                        {
                            "title_llm_pending": False,
                            "title_seed": "",
                            "title_assistant": "",
                        },
                        touch=False,
                    )
                    continue
                _session_meta_patch(
                    slug, sid, {"title_assistant": assistant}, touch=False
                )
            spawn_title_gen(sid, "")
            count += 1
    if count:
        _log.info("maintenance_title_requeued sessions=%d", count)
    return count


# ---- one-time purge of stale empty sessions ------------------------------ #

# DATA-05 was a product decision (2026-10-23): the 15 sidebar entries that had
# a title but zero messages for >1 day are junk from failed scheduled runs and
# abandoned tests. Deleted ONCE — a permanent "delete everything empty" rule
# would silently eat a legitimate draft the user comes back to.
_MAINTENANCE_STATE = "maintenance.json"
_EMPTY_PURGE_FLAG = "stale_empty_sessions_purged"


def _state_path():
    return paths.home() / _MAINTENANCE_STATE


def _read_state() -> dict:
    try:
        import json

        data = json.loads(_state_path().read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    import json

    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        _log.exception("maintenance_state_write_failed")


def _has_messages(slug: str, session_id: str) -> bool:
    from .checkpointer import FileCheckpointer

    try:
        tup = FileCheckpointer(slug).get_tuple(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:  # noqa: BLE001 — unreadable checkpoint: keep the session
        return True
    if not tup or not tup.checkpoint:
        return False
    return bool(
        (tup.checkpoint.get("channel_values") or {}).get("messages")
    )


def _remove_session_files(slug: str, session_id: str) -> None:
    """Drop the checkpoint side-cars; keep the workspace if it still holds
    user data (some empty sessions were created for a scheduled run that
    uploaded files before dying — those uploads must not vanish)."""
    import shutil

    base = paths.project_sessions_dir(slug)
    for suffix in (".json", ".world.json", ".projects.json"):
        try:
            (base / f"{session_id}{suffix}").unlink()
        except OSError:
            pass
    sess_dir = base / session_id
    if not sess_dir.is_dir():
        return
    has_files = any(f for _, _, files in os.walk(sess_dir) for f in files)
    if has_files:
        _log.info("maintenance_empty_session_kept_files sid=%s", session_id)
        return
    try:
        shutil.rmtree(sess_dir)
    except OSError:
        _log.exception("maintenance_empty_session_dir_failed sid=%s", session_id)


def _purge_stale_empty_sessions_once() -> None:
    state = _read_state()
    if state.get(_EMPTY_PURGE_FLAG):
        return
    removed = 0
    for slug in _project_slugs():
        rows = _session_meta_list(slug)
        keep: list[dict] = []
        victim_ids: list[str] = []
        for m in rows:
            sid = str(m.get("id") or "")
            # 24h grace: a session created while the user is still typing has
            # no messages yet and must survive.
            if sid and time.time() - float(m.get("created") or 0) > 86400:
                if not _has_messages(slug, sid):
                    victim_ids.append(sid)
                    continue
            keep.append(m)
        if not victim_ids:
            continue
        _session_meta_rewrite_all(slug, keep)
        for sid in victim_ids:
            _remove_session_files(slug, sid)
        removed += len(victim_ids)
        _log.info(
            "maintenance_empty_sessions_purged slug=%s count=%d", slug, len(victim_ids)
        )
    state[_EMPTY_PURGE_FLAG] = True
    _write_state(state)
    if removed:
        _log.info("maintenance_empty_sessions_purged_total=%d", removed)


# ---- entry point ---------------------------------------------------------- #


def run_startup_maintenance() -> None:
    """Synchronous sweep (usable before the event loop serves requests)."""
    slugs = _project_slugs()
    if not slugs:
        return
    valid_sessions = _valid_session_ids()
    try:
        _purge_stale_empty_sessions_once()
        valid_sessions = _valid_session_ids()  # artifacts may now be orphaned
    except Exception:  # noqa: BLE001 — purge is optional; never block startup
        _log.exception("maintenance_empty_sessions_failed")
    for slug in slugs:
        try:
            _heal_session_metas(slug)
        except Exception:  # noqa: BLE001 — one bad project must not stop others
            _log.exception("maintenance_session_index_failed slug=%s", slug)
        try:
            _prune_artifacts(slug, valid_sessions)
        except Exception:  # noqa: BLE001
            _log.exception("maintenance_artifacts_failed slug=%s", slug)
