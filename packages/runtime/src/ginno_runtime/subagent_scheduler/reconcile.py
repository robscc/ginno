"""Reconciliation paths: the startup orphan backstop (§5.7) and the
redelivery of unabsorbed steer-stash result injections (major-1)."""

from __future__ import annotations

import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


def reconcile_orphan_subagents() -> int:
    """Startup backstop (design §5.7): at lifespan start no background task of
    the previous process survives — no turn task, no queued wake, no settle
    hook — so every LIVE (running/waiting) subagent meta on disk is a strand.
    Left as-is, a ghost ``running`` meta consumes a concurrency slot forever
    (the cap counts status=="running" metas) and shows a live sidebar dot no
    stop machinery will ever clear (nothing observes an event; only a manual
    user stop would settle it), and a ghost ``waiting`` parent is stranded
    with no descendant left able to wake it.

    Quietly settle each to ``stopped``: meta patch only — no summary, no
    result injection, no waiting-parent wake (the parents are stranded too
    and this same walk settles them, so order does not matter and no wake
    turn may be opened at boot). No sockets are connected yet; the sidebar
    reads the settled metas when the UI connects. Returns the count settled.
    Best-effort like the workflow-run reconciliation it mirrors."""
    from ..session_meta import _session_meta_list

    settled = 0
    try:
        indexes = list(_NS.paths.home().glob("projects/*/sessions/_index.json"))
    except Exception:
        _NS._log.exception("subagent_reconcile_glob_failed")
        return 0
    for idx in indexes:
        slug = idx.parent.parent.name
        try:
            metas = _session_meta_list(slug)
        except Exception:
            _NS._log.exception("subagent_reconcile_list_failed slug=%s", slug)
            continue
        for m in metas:
            if not _NS._is_subagent(m) or _NS._subagent_status(m) not in _NS.LIVE_STATUSES:
                continue
            try:
                sa = dict(m.get("subagent") or {})
                sa["status"] = "stopped"
                _NS._session_meta_patch(slug, m["id"], {"subagent": sa})
                settled += 1
                _NS._log.info(
                    "subagent_orphan_settled session=%s was=%s",
                    m["id"], _NS._subagent_status(m),
                )
            except Exception:
                _NS._log.exception(
                    "subagent_reconcile_patch_failed session=%s", m.get("id"),
                )
    if settled:
        _NS._log.info("subagent_orphans_settled count=%d", settled)
    return settled


def _is_subagent_injection(entry: dict) -> bool:
    """A steer-stash entry the SCHEDULER enqueued (not a user steer): it carries
    the ``ginno_subagent_result`` marker in ``extra_kwargs`` and non-empty text.
    Only these are reconciled on redelivery — ordinary user steers keep the
    frontend's own turn-end resend semantics (steering-design §3.3)."""
    return bool((entry.get("extra_kwargs") or {}).get("ginno_subagent_result")) and bool(
        (entry.get("text") or "").strip()
    )


def reconcile_stash_injections(session_id: str) -> int:
    """Redeliver UNABSORBED subagent result injections through the wake-turn
    channel (major-1 reconciliation).

    A result injected into a RUNNING parent rides the steer stash — but the
    stash is cleared at the end of every turn segment regardless of absorption,
    on the "the client re-sends what it never saw acknowledged" contract. The
    scheduler never re-sends, so an injection that lands after the parent's
    LAST agent superstep (final reply already streaming) — or into a stash the
    user's stop of a parked turn drops (_stop_parked_turn) — would be lost
    forever. This pulls the tagged entries out of the stash and wakes the
    parent with each one; idempotent (entries leave the stash when handed to
    the wake channel), so calling it from both the turn-segment finally and the
    settle hook is safe.

    Called synchronously BEFORE the generic steer_clear at those two sites.
    """
    stash = _NS._STEER_STASH.get(session_id) or []
    tagged = [e for e in stash if _is_subagent_injection(e)]
    if not tagged:
        return 0
    _NS._STEER_STASH[session_id] = [e for e in stash if not _is_subagent_injection(e)]
    if not _NS._STEER_STASH[session_id]:
        _NS._STEER_STASH.pop(session_id, None)
    for entry in tagged:
        child_id = (entry.get("extra_kwargs") or {}).get("ginno_subagent_result")
        _NS._log.info(
            "subagent_result_reconciled parent=%s child=%s", session_id, child_id
        )
        _NS.spawn_bg(
            _NS._wake_parent_turn(session_id, entry["text"], entry.get("extra_kwargs"))
        )
    return len(tagged)
