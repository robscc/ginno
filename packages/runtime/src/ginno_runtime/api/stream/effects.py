"""Per-turn side effects: file/preview touches, code.changed, auto-distill,
and citation accounting.

Verbatim move out of ``engine.py`` (pure structural split — no behavior
change). ``engine`` re-imports these names so ``engine.X`` and the package
re-export surface keep working, including the monkeypatch mirror.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ginno_runtime import jsonx as json

from ... import agents as agents_reg
from ... import artifacts as art_store
from ... import files as files_mod
from ... import paths, usage_store
from ... import server_shared as shared
from ... import vibe_island as _vibe_island
from ... import workflows as wf_store
from ...checkpointer import ABANDONED_TURNS
from ...graph import BLOCK_PREFIX
from ...knowledge import citations as cit
from ...lang import t
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _SESSIONS,
    _TURN_STOP,
    _USAGE_BY_SESSION,
    _WF_RUN_TASKS,
    _ensure_turn_log,
    _ev,
    _log,
    _push_global_event,
    _push_session_event,
    _try_send,
    _turn_lock,
    spawn_bg,
)
from ...session_meta import _find_meta, _session_meta_patch
from ...session_unread import note_turn_finished
from ...todos import store as todo_store
from ...tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ...tools.ask_tools import begin_ask_budget
from ...tools.render_tools import (
    RENDER_TOOL_NAMES,
    AttachTagStreamFilter,
    widget_event,
)
from ...tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES
from ...usage import add_usage, cache_hit_ratio, empty_usage, extract_usage
from ...workflows import store as wf_storemod
from .. import server_tools
from ..files import _normalize_file_ref, _register_artifact_file, _session_workspace
from ..messages_ui import _tool_args_preview, _tool_content_str, _truncate_for_ws


async def _tool_file_effects(
    safe_send, emit, slug: str, session_id: str, name_args: tuple[str, dict] | None, content: str
) -> None:
    """After a tool finishes, keep file previews live (docs §7.5):

    1. Structured tools declare their path arg → ``registry.touch`` → the UI
       gets ``preview.invalidate`` for that file.
    2. Opaque tools (bash / MCP) → best-effort: any registered path appearing
       in the tool args is touched.
    """
    if not slug or not name_args:
        return
    name, args = name_args
    reg = files_mod.get_registry(slug)
    touched: list[str] = []

    if name in ("write_file", "edit_file", "read_file", "parse_document"):
        p = (args or {}).get("path")
        if p:
            pp = Path(p).expanduser()
            if not pp.is_absolute():
                # The builtin tools bind relative paths to the session
                # workspace (builtin._ws), not the sidecar cwd — resolve
                # identically, or touch never matches the registered entry.
                pp = Path(str(_session_workspace(slug, session_id) / p)).expanduser()
            touched.append(files_mod.norm_path(str(pp)))
    elif args:
        try:
            blob = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            blob = str(args)
        for e in reg.list_session(session_id) or reg.list_all():
            if e.get("path") and e["path"] in blob:
                touched.append(e["path"])

    # Code-generated images (inline-images design): the bash tool appends a
    # machine marker listing pictures the command wrote into the workspace.
    # Register each one (kind="image"), surface it as an artifact, and
    # broadcast ``image.emit`` so the chat renders it inline. The frontend
    # builds the URL from file_id (BASE-aware); mtime busts the browser cache
    # when a same-named image is regenerated.
    if name == "bash" and content:
        from ...files.images import parse_images_marker

        for p in parse_images_marker(content):
            pp = Path(p).expanduser()
            if not pp.is_file():
                continue
            norm_ref = files_mod.norm_path(str(pp))
            is_new = reg.find_by_path(norm_ref) is None
            art = art_store.add_artifact(slug, "image", pp.name, norm_ref, session_id)
            entry = reg.register(
                pp.name, str(pp), kind="image", session_id=session_id,
                artifact_id=art.get("id"),
            )
            if is_new:
                await safe_send(emit("artifacts.changed", {}))
            try:
                mtime = int(pp.stat().st_mtime)
            except OSError:
                mtime = 0
            await safe_send(
                emit("image.emit", {"file_id": entry["id"], "name": entry["name"], "mtime": mtime})
            )
            touched.append(norm_ref)

    seen: set[str] = set()
    for p in touched:
        if p in seen:
            continue
        seen.add(p)
        for e in files_mod.touch(p, reason=f"tool:{name}"):
            await safe_send(
                emit("preview.invalidate", {"file_id": e["id"], "reason": f"tool:{name}"})
            )


async def _emit_code_changes(safe_send, emit, slug: str, session_id: str, content) -> None:
    """Broadcast ``code.changed`` for every file a tool result just wrote.

    ``write_file`` / ``edit_file`` append a
    ``<!--ginno-code:[{path,op,version}]-->`` trailer (``files/code_changes.py``,
    code-panel S3 design §4.2-3). One event per file: the tree marks it, an open
    CLEAN tab reloads, an open DIRTY tab fills S2's conflict bar (never
    auto-overwritten, brief §1-5) — which is why the event carries the
    post-write ``version`` (brief §0).

    ``root_id`` is best-effort: longest prefix against the session's roots, or
    ``null`` when the file sits outside every root (the panel then has nowhere
    to place it). Decoration must never break a turn, so the lookup is guarded.
    """
    from ...files.code_changes import match_root_id, parse_code_marker

    changes = parse_code_marker(content)
    if not changes:
        return

    roots: list[dict] = []
    try:
        # Read-only reuse of the code panel's own session context — the exact
        # root list the panel serves to the frontend, so both sides agree on
        # what "the roots" are. Imported lazily like the other feature-local
        # imports in this module: a module-level import would couple the WS
        # layer to the router module for one best-effort lookup.
        from .. import code as code_panel

        ctx = code_panel._session_ctx(slug, session_id) if slug and session_id else None
        if ctx is not None:
            roots = code_panel._list_roots(ctx)
    except Exception as e:  # noqa: BLE001 — decoration only: never fail the turn
        _log.warning("code_changed_root_lookup session=%s err=%s", session_id, e)
        roots = []

    for ch in changes:
        await safe_send(
            emit(
                "code.changed",
                {
                    "session_id": session_id,
                    "path": ch["path"],
                    "op": ch["op"],
                    "version": ch.get("version") or "",
                    "root_id": match_root_id(ch["path"], roots),
                },
            )
        )


# Auto-distill (world-state-plan A5b, revived dead config): when the pool
# reaches pool_flush_threshold, distill a DRAFT in the background. Draft mode
# guarantees even the automatic path never silently overwrites MEMORY.md — the
# user reviews the diff. Failures are throttled so a broken provider can't
# burn tokens on every turn.
_AUTO_DISTILL_FAIL_AT: float = 0.0
_AUTO_DISTILL_THROTTLE_S = 600.0


def _maybe_auto_distill(cfg) -> None:
    global _AUTO_DISTILL_FAIL_AT
    if not cfg.auto_summarize:
        return
    if time.time() < _AUTO_DISTILL_FAIL_AT:
        return
    try:
        from ...memory import has_draft, pool_count

        if has_draft():
            return  # a draft is already awaiting review
        if pool_count() < int(cfg.pool_flush_threshold or 30):
            return
    except Exception:
        return

    async def _auto_distill_task() -> None:
        global _AUTO_DISTILL_FAIL_AT
        try:
            from ...memory import create_draft

            result = await create_draft(trigger="auto")
            if result.get("ok") and result.get("draft"):
                await _push_global_event("memory.changed", {"draft": True})
            elif not result.get("ok") and not result.get("skipped"):
                _AUTO_DISTILL_FAIL_AT = time.time() + _AUTO_DISTILL_THROTTLE_S
                _log.warning("auto_distill_failed error=%s", result.get("error"))
        except Exception:
            _AUTO_DISTILL_FAIL_AT = time.time() + _AUTO_DISTILL_THROTTLE_S
            _log.exception("auto_distill_failed")

    spawn_bg(_auto_distill_task())


async def _process_turn_citations(session_id: str, turn_id: str, text: str) -> int:
    """Parse the trailing ``<ginno_citations>`` block, validate it against the
    turn's registered sources, and record the wiki usage ledger
    (docs/citations-design.md §2-3). Telemetry-only: never raises outward.

    Returns the number of verified citations — a quality signal for the
    memory pool (cited turns carry more evidence weight when distilling).
    """
    from ...knowledge import citations as cit
    from ...knowledge import usage as kb_usage
    from ...knowledge import web_usage
    from ...knowledge.config import load_knowledge_config

    sources = cit.end_turn_sources(session_id)  # always pop — turn is over
    if not text:
        return 0
    cfg = load_knowledge_config()
    if not getattr(cfg, "citations", True):
        return 0
    entries = cit.parse_citation_block(text)
    if not entries:
        return 0

    resolve_wiki = None
    if cfg.usable:
        def resolve_wiki(ref: str):  # noqa: E306 — index lookup for index_only triage
            try:
                from ...knowledge.indexer import get_indexer

                idx = get_indexer(cfg.vault_path, cfg.rescan_interval_s)
                key = cit._norm_wiki_ref(ref)
                want_title = ref.strip().lower()
                for e in idx.get_entries():
                    if cit._norm_wiki_ref(e.relative_path) == key:
                        return e.relative_path
                    if (e.title or "").strip().lower() == want_title:
                        return e.relative_path
            except Exception:
                pass
            return None

    validated = cit.validate_citations(entries, sources, resolve_wiki=resolve_wiki)
    invalid: list[str] = []
    web_cited = 0
    for item in validated:
        kind = item.get("kind")
        status = item.get("status")
        ref = item.get("identity") or item.get("ref") or ""
        if kind == "wiki":
            if status == "verified":
                kb_usage.record_cited(ref, session_id, turn_id)
            elif status == "index_only":
                kb_usage.record_cited(ref, session_id, turn_id, index_only=True)
            else:
                invalid.append(item.get("ref") or "")
        elif kind == "web" and status == "verified":
            # Web ledger (citations-design.md §4.6): credit domain + engine.
            # NOTE: no `fetched` flag here — web_fetch already called
            # record_fetched at fetch time; passing it again would double-count
            # the domain's fetched counter.
            try:
                web_usage.record_cited(ref, engine=item.get("engine") or "")
                web_cited += 1
            except Exception:
                _log.exception("web_usage_cited_failed session=%s", session_id)
    if invalid:
        kb_usage.record_invalid(invalid)
    verified = sum(1 for i in validated if i.get("status") == "verified")
    _log.info(
        "turn_citations session=%s turn=%s entries=%d verified=%d invalid=%d",
        session_id,
        turn_id,
        len(validated),
        verified,
        len(invalid),
    )
    return verified
