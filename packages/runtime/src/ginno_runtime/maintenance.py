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

# P1-1 archive rule: an inactive one-shot Q&A leaves the sidebar for the
# collapsed "Archived" bucket. A session with real traffic stays visible.
ARCHIVE_AFTER_DAYS = 14
ARCHIVE_MAX_MESSAGES = 2
# Bump when the judging rule changes: auto-written flags from an older rule
# are discarded and re-judged; manual archive choices are never touched.
ARCHIVE_RULE_VERSION = 3


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


def _backfill_last_success() -> None:
    """P0-1: seed ``last_success`` from the run history.

    The field only starts being written on the next successful run; without a
    backfill every task would read "从未成功" until it happens to run again,
    which is the opposite of what the card is for. Idempotent — tasks that
    already carry the field are left alone.
    """
    try:
        from . import schedule_store

        rows = schedule_store.query_runs(page=1, page_size=500).get("rows") or []
        best: dict[str, tuple[float, str]] = {}
        for r in rows:
            if r.get("status") != "ok":
                continue
            tid = str(r.get("task_id") or "")
            ts = float(r.get("finished_at") or r.get("started_at") or 0)
            if tid and ts > best.get(tid, (0.0, ""))[0]:
                best[tid] = (ts, str(r.get("run_id") or ""))
        for task in schedule_store.list_tasks():
            tid = str(task.get("id") or "")
            if not tid or task.get("last_success") or tid not in best:
                continue
            ts, rid = best[tid]
            schedule_store.patch_task(
                tid, {"last_success": {"run_id": rid, "finished": ts}}
            )
    except Exception:  # noqa: BLE001 — display-only backfill
        _log.exception("maintenance_last_success_backfill_failed")


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
        from ginno_runtime import jsonx as json

        data = json.loads(_state_path().read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    from ginno_runtime import jsonx as json

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

    from .checkpointer import legacy_checkpoint_path, checkpoint_path

    base = paths.project_sessions_dir(slug)
    for p in (
        checkpoint_path(slug, session_id),
        legacy_checkpoint_path(slug, session_id),
        base / f"{session_id}.world.json",
        base / f"{session_id}.projects.json",
    ):
        try:
            p.unlink()
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


def session_reference_map() -> dict[str, list[str]]:
    """session_id → human-readable list of records that point at it.

    Used by the purge (skip), the delete guard (refuse) and diagnostics
    (report). Teams and scheduled run logs are the only across-store refs.
    """
    refs: dict[str, list[str]] = {}

    def _add(sid: str, label: str) -> None:
        if sid:
            refs.setdefault(str(sid), []).append(label)

    try:
        from .teams import store as team_store

        for t in team_store.list_teams():
            name = str(t.get("name") or t.get("id") or "?")
            _add(str(t.get("main_session_id") or ""), f"team {name} (main)")
            for m in t.get("members") or []:
                _add(str(m.get("session_id") or ""), f"team {name} ({m.get('display_name') or 'member'})")
                for sid in m.get("session_ids") or []:
                    _add(str(sid or ""), f"team {name} ({m.get('display_name') or 'member'})")
    except Exception:  # noqa: BLE001 — a broken teams store must not skip hygiene
        _log.exception("maintenance_team_refs_failed")
    try:
        from ginno_runtime import jsonx as json

        for p in paths.schedule_runs_dir().glob("runs-*.jsonl"):
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in lines:
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict):
                    continue
                rid = str(e.get("run_id") or "?")
                _add(str(e.get("session_id") or ""), f"schedule run {rid}")
                _add(str(e.get("forked_session_id") or ""), f"schedule run {rid} (fork)")
    except Exception:  # noqa: BLE001
        _log.exception("maintenance_schedule_refs_failed")
    return refs


def _referenced_session_ids() -> set[str]:
    """Sessions some other record points at — teams and scheduled run logs.

    The empty-session purge must never delete one of these: the reference is
    left dangling and every consumer (team chat, “view session” in run
    history) breaks silently (DATA-06).
    """
    return set(session_reference_map())


def _message_count(slug: str, session_id: str) -> int | None:
    """How many USER turns the session has (None = checkpoint unreadable).

    Counting raw messages was wrong for the archive rule: one question with a
    few tool calls is already 10+ raw messages, so almost nothing looked like
    a one-shot. The backlog's "1–2 条消息" means conversational turns — count
    human messages, skipping the injected context/summary/re-injection rows.
    """
    try:
        from .checkpointer import FileCheckpointer
        from .world_state import (
            REINJECT_MSG_PREFIX,
            SUMMARY_MSG_PREFIX,
            TURN_CONTEXT_PREFIX,
        )

        tup = FileCheckpointer(slug).get_tuple(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:  # noqa: BLE001 — unreadable: leave the flag untouched
        return None
    if not tup or not tup.checkpoint:
        return 0
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    injected = (TURN_CONTEXT_PREFIX, SUMMARY_MSG_PREFIX, REINJECT_MSG_PREFIX)
    turns = 0
    for m in messages:
        if getattr(m, "type", None) != "human":
            continue
        text = m.content if isinstance(m.content, str) else ""
        if text and text.startswith(injected):
            continue
        turns += 1
    return turns


def _mark_archived_sessions() -> None:
    """P1-1: older, near-empty sessions move to the collapsed archive bucket.

    Runs at most once a day (startup can happen many times). Only sessions
    without an explicit ``archived`` value are judged, so a manual
    archive/unarchive always wins; sessions referenced by a team or a
    scheduled run are skipped — hiding them would make those references look
    broken to the user.
    """
    state = _read_state()
    now = time.time()
    if state.get("archived_rule_version") != ARCHIVE_RULE_VERSION:
        # Re-judge flags written by an older rule (they are auto flags: the
        # bulk/manual path never sets ``archived_auto``).
        for slug in _project_slugs():
            rows = _session_meta_list(slug)
            dirty = False
            for m in rows:
                # v1/v2 wrote ``archived: false`` without ``archived_auto``;
                # both are auto decisions and must be re-judged under the new
                # rule. ``archived: true`` (a real archive) is left alone.
                if m.get("archived_auto") or m.get("archived") is False:
                    m.pop("archived", None)
                    m.pop("archived_auto", None)
                    dirty = True
            if dirty:
                _session_meta_rewrite_all(slug, rows)
        state["archived_rule_version"] = ARCHIVE_RULE_VERSION
        state["archived_checked_at"] = 0
        _write_state(state)
    if now - float(state.get("archived_checked_at") or 0) < 86400:
        return
    referenced = set(session_reference_map())
    changed_projects = 0
    for slug in _project_slugs():
        rows = _session_meta_list(slug)
        changed = False
        for m in rows:
            if "archived" in m or m.get("pinned"):
                continue
            sid = str(m.get("id") or "")
            if not sid or sid in referenced:
                continue
            if now - float(m.get("updated") or 0) < ARCHIVE_AFTER_DAYS * 86400:
                continue
            count = _message_count(slug, sid)
            if count is None:
                continue
            m["archived"] = count <= ARCHIVE_MAX_MESSAGES
            m["archived_auto"] = True
            changed = True
        if changed:
            _session_meta_rewrite_all(slug, rows)
            changed_projects += 1
    state["archived_checked_at"] = now
    _write_state(state)
    if changed_projects:
        _log.info("maintenance_archived_sessions projects=%d", changed_projects)


def _purge_stale_empty_sessions_once() -> None:
    state = _read_state()
    if state.get(_EMPTY_PURGE_FLAG):
        return
    referenced = _referenced_session_ids()
    removed = 0
    purged: list[str] = []
    for slug in _project_slugs():
        rows = _session_meta_list(slug)
        keep: list[dict] = []
        victim_ids: list[str] = []
        for m in rows:
            sid = str(m.get("id") or "")
            # 24h grace: a session created while the user is still typing has
            # no messages yet and must survive.
            if (
                sid
                and sid not in referenced
                and time.time() - float(m.get("created") or 0) > 86400
            ):
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
        purged.extend(victim_ids)
        _log.info(
            "maintenance_empty_sessions_purged slug=%s count=%d", slug, len(victim_ids)
        )
    state[_EMPTY_PURGE_FLAG] = True
    if purged:
        # Audit trail: which sessions vanished (DATA-06 ask). Keeps the ids
        # recoverable from memory/maintenance.json even though the files are
        # gone.
        state["empty_sessions_purged_ids"] = purged
    _write_state(state)
    if removed:
        _log.info(
            "maintenance_empty_sessions_purged_total=%d ids=%s",
            removed,
            ",".join(i[:8] for i in purged[:20]),
        )


def _heal_dangling_refs(valid_sessions: set[str]) -> None:
    """Clear references to sessions that no longer exist (DATA-06).

    * a team whose main session AND every member session are gone is deleted
      (nothing is left to run);
    * otherwise dead members are removed from the roster and a dead
      ``main_session_id`` is cleared with ``main_session_missing`` set, so the
      UI can say "session deleted" instead of opening a blank page;
    * scheduled run rows keep their record but lose the dead ``session_id`` /
      ``forked_session_id``.
    """
    try:
        from .teams import store as team_store

        for t in team_store.list_teams():
            tid = str(t.get("id") or "")
            if not tid:
                continue
            # P2-3: legacy teams were created with an empty shared workspace;
            # give them the same per-team dir new teams get.
            sw = dict(t.get("shared_workspace") or {})
            if not str(sw.get("workspace") or "").strip():
                ws_dir = (
                    paths.home()
                    / "projects"
                    / str(t.get("project_slug") or "default")
                    / "teams"
                    / tid
                )
                try:
                    ws_dir.mkdir(parents=True, exist_ok=True)
                    sw["workspace"] = str(ws_dir)
                    team_store.update_team(tid, {"shared_workspace": sw})
                    _log.info("maintenance_team_workspace_set team=%s", tid)
                except OSError:
                    pass
            main = str(t.get("main_session_id") or "")
            members = t.get("members") or []
            missing_main = bool(main) and main not in valid_sessions
            dead: list[str] = []
            for m in members:
                iid = str(m.get("instance_id") or "")
                sids = [str(s) for s in (m.get("session_ids") or []) if s]
                if not sids and m.get("session_id"):
                    sids = [str(m["session_id"])]
                if sids and all(s not in valid_sessions for s in sids) and iid:
                    dead.append(iid)
            if missing_main and len(dead) == len(members):
                team_store.delete_team(tid)
                _log.info("maintenance_team_removed team=%s reason=all_sessions_gone", tid)
                continue
            for iid in dead:
                team_store.remove_member(tid, iid)
                _log.info("maintenance_team_member_removed team=%s member=%s", tid, iid)
            if missing_main:
                team_store.update_team(
                    tid, {"main_session_id": "", "main_session_missing": True}
                )
                _log.info("maintenance_team_main_cleared team=%s", tid)
    except Exception:  # noqa: BLE001 — one bad team must not stop the sweep
        _log.exception("maintenance_team_heal_failed")

    try:
        from . import schedule_store

        fixed = schedule_store.heal_dead_session_refs(valid_sessions)
        if fixed:
            _log.info("maintenance_schedule_refs_healed refs=%d", fixed)
    except Exception:  # noqa: BLE001
        _log.exception("maintenance_schedule_heal_failed")


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
    try:
        _heal_dangling_refs(valid_sessions)
    except Exception:  # noqa: BLE001
        _log.exception("maintenance_ref_heal_failed")
    try:
        _backfill_last_success()
    except Exception:  # noqa: BLE001
        _log.exception("maintenance_last_success_failed")
    try:
        _mark_archived_sessions()
    except Exception:  # noqa: BLE001 — archiving is cosmetic; never block start
        _log.exception("maintenance_archive_failed")
    for slug in slugs:
        try:
            _heal_session_metas(slug)
        except Exception:  # noqa: BLE001 — one bad project must not stop others
            _log.exception("maintenance_session_index_failed slug=%s", slug)
        try:
            _prune_artifacts(slug, valid_sessions)
        except Exception:  # noqa: BLE001
            _log.exception("maintenance_artifacts_failed slug=%s", slug)
