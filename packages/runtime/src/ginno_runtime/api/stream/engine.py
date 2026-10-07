"""The streaming engine: _stream_graph, stop/heal, transient auto-retry.

Split out of the former single-module ``api/stream.py`` (pure structural
move — no behavior change). The package ``__init__`` re-exports the whole
historical namespace and mirrors patch writes into the submodules, so
``ginno_runtime.api.stream.X`` keeps working as the import/patch surface.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ... import agents as agents_reg
from ... import artifacts as art_store
from ... import files as files_mod
from ... import paths
from ... import server_shared as shared
from ... import usage_store
from ... import workflows as wf_store
from ...checkpointer import ABANDONED_TURNS
from ...graph import BLOCK_PREFIX
from ...lang import t
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _SESSION_WS,
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
from ...todos import store as todo_store
from ...tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ...tools.ask_tools import begin_ask_budget
from ...tools.render_tools import RENDER_TOOL_NAMES, widget_event
from ...tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES
from ...usage import add_usage, cache_hit_ratio, empty_usage, extract_usage
from ...workflows import store as wf_storemod
from ..files import _normalize_file_ref, _register_artifact_file, _session_workspace
from ..messages_ui import _tool_args_preview, _tool_content_str, _truncate_for_ws
from ..workflows import _run_workflow_bg, _spawn_run_task

async def _tool_file_effects(
    safe_send, emit, slug: str, session_id: str, name_args: tuple[str, dict] | None, content: str
) -> None:
    """After a tool finishes, keep file previews live (docs §7.5):

    1. Structured tools declare their path arg → ``registry.touch`` → the UI
       gets ``preview.invalidate`` for that file.
    2. Opaque tools (bash / MCP) → best-effort: any registered path appearing
       in the tool args is touched.
    3. ``analyze_table`` table results → register the derived CSV as an
       artifact and emit ``preview.emit {open: true}`` so the result sheet
       opens automatically in the UI.
    """
    if not slug or not name_args:
        return
    name, args = name_args
    reg = files_mod.get_registry(slug)
    touched: list[str] = []

    if name in ("write_file", "edit_file", "read_file", "parse_document", "analyze_table"):
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

    if name == "analyze_table" and content:
        try:
            d = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            d = None
        dp = d.get("derived_path") if isinstance(d, dict) and d.get("ok") else None
        if dp and Path(dp).is_file():
            # Relocate the derived CSV into the session's results/ dir so every
            # session artifact lives under sessions/<sid>/ — the tool writes it
            # next to the source file, which may sit outside the session dir
            # (e.g. an external path the user analyzed). Happens in-turn, before
            # registration, so the artifact is stored at its final path.
            import shutil

            results_dir = paths.session_results_dir(slug, session_id)
            try:
                results_dir.mkdir(parents=True, exist_ok=True)
                final = files_mod.unique_dest(results_dir / Path(dp).name)
                shutil.move(str(dp), str(final))
                dp = str(final)
            except OSError:
                pass  # keep the tool's original location if the move fails
            norm_ref = files_mod.norm_path(dp)
            art = art_store.add_artifact(slug, "file", Path(dp).name, norm_ref, session_id)
            entry = reg.register(
                Path(dp).name, dp, kind="table", session_id=session_id, artifact_id=art.get("id")
            )
            await safe_send(
                emit(
                    "preview.emit",
                    {
                        "file_id": entry["id"],
                        "name": entry["name"],
                        "path": entry["path"],
                        "kind": "table",
                        "open": True,
                    },
                )
            )
            await safe_send(emit("artifacts.changed", {}))
            touched.append(norm_ref)

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


# Max seconds between stream chunks before the stall watchdog aborts the turn
# (see chunked_stream in _stream_graph). Module-level so tests can shrink it.
CHUNK_TIMEOUT_S = 180.0

# LangGraph superstep budget per turn. The framework default (25) caps a turn
# at roughly a dozen tool rounds — research-style subagent turns (many
# web_search/fetch rounds) blow through it and die with GraphRecursionError.
# Applied as a default in _stream_graph so every path (WS invoke/resume/retry,
# scheduler-managed subagent turns, wake turns) gets it. Config: env
# GINNO_RECURSION_LIMIT wins (tests), else settings runtime.recursion_limit,
# else 128. Read per turn so a settings write takes effect without restart.
TURN_RECURSION_LIMIT_DEFAULT = 128


def turn_recursion_limit() -> int:
    raw: str | None = os.environ.get("GINNO_RECURSION_LIMIT")
    if raw and raw.strip():
        try:
            return max(25, min(1000, int(raw.strip())))
        except ValueError:
            _log.warning("recursion_limit_env_invalid value=%r", raw)
    try:
        p = paths.settings_path()
        if p.exists():
            settings = json.loads(p.read_text() or "{}")
            val = (
                (settings.get("runtime") or {})
                if isinstance(settings, dict)
                else {}
            ).get("recursion_limit")
            if val is not None:
                return max(25, min(1000, int(val)))
    except (OSError, ValueError, TypeError):
        _log.info("recursion_limit_settings_unreadable", exc_info=True)
    return TURN_RECURSION_LIMIT_DEFAULT


def format_turn_error(e: BaseException) -> str:
    """User-facing text for the error card (the fallback ``message`` field).

    The raw exception dump stays the English fallback (i18n-design.md §3), but
    known failures get a Ginno-specific remedy appended — the framework's own
    advice is not actionable inside Ginno (LangGraph's GraphRecursionError
    points at a langchain docs page instead of this app's settings file).
    Substring gates elsewhere (e.g. subagent_scheduler's recursion wrap) keep
    matching because the class name stays in the first line.
    """
    return turn_error_fields(e)["message"]


def turn_error_fields(e: BaseException) -> dict:
    """Full error-event payload fields (i18n-design.md §3): ``message``
    (English fallback text) + ``i18n_key`` + ``params`` so the web UI renders
    a localized version; a stale bundle missing the key falls back to
    ``message``. Control-flow signals (GraphBubbleUp family,
    NodeCancelledError) keep the generic key with no hint — they are not
    failures, and a "what to do next" tip would mislead.
    """
    text = f"{type(e).__name__}: {e}"
    hit = _langgraph_error_hint(e)
    if hit is None:
        return {
            "message": text,
            "i18n_key": "stream.turn_failed",
            "params": {"error": text},
        }
    suffix, params, hint = hit
    return {
        "message": f"{text}\n{hint}",
        "i18n_key": f"stream.turn_failed.{suffix}",
        "params": {"error": text, **params},
    }


def _langgraph_error_hint(e: BaseException) -> tuple[str, dict, str] | None:
    """Classify a langgraph turn failure for the error card.

    Returns ``(i18n key suffix under stream.turn_failed, params, English
    hint)`` or ``None`` for unknown / control-flow exceptions.
    """
    from langgraph.errors import (  # lazy: import cost
        EmptyChannelError,
        EmptyInputError,
        GraphBubbleUp,
        GraphRecursionError,
        InvalidUpdateError,
        NodeCancelledError,
        NodeTimeoutError,
    )

    # GraphRecursionError subclasses GraphBubbleUp — test before the family.
    if isinstance(e, GraphRecursionError):
        limit = turn_recursion_limit()
        return (
            "recursion_limit",
            {"limit": limit},
            f"This turn hit Ginno's step budget (recursion_limit={limit})."
            "\nTo raise it, edit ~/.ginno/settings.json:"
            '\n{ "runtime": { "recursion_limit": 256 } }'
            "\nThe new value applies from the next turn — no restart needed.",
        )
    # Control flow, not failure: interrupts / commands / drains / user stop.
    if isinstance(e, (GraphBubbleUp, NodeCancelledError)):
        return None
    if isinstance(e, EmptyInputError):
        return (
            "empty_input",
            {},
            "This turn had no state to resume — the first model call never"
            " completed (common after an app restart or a failed first call)."
            "\nUse retry on the error card; it re-runs the original input.",
        )
    if isinstance(e, (InvalidUpdateError, EmptyChannelError)):
        return (
            "state",
            {},
            "A step hit inconsistent turn state — usually a checkpoint written"
            " by an older version or left mid-write by a crash."
            "\nRetry once; if it repeats, restart the app, then start a new"
            " session if it still repeats"
            " (traceback: ~/.ginno/logs/sidecar.log).",
        )
    if isinstance(e, NodeTimeoutError):
        return (
            "node_timeout",
            {},
            "A step exceeded its time budget — usually a slow provider or tool"
            " call, not a Ginno fault."
            "\nRetry; if it repeats, check the provider's status"
            " (traceback: ~/.ginno/logs/sidecar.log).",
        )
    return None


# --- Turn-level auto retry for TRANSIENT provider/network failures ----------
# 2026-09-29 incident (turn f50a6304): a 100k-token-context turn died twice in
# a row — first ssl SSLV3_ALERT_BAD_RECORD_MAC mid-stream, then the manual
# checkpoint retry stalled 180s in prefill — while the endpoint itself was
# healthy minutes later (verified by replaying a 123k-token stream). Same
# "model/stream stall" pattern on 09-09/09-10 with a DIFFERENT provider, so the
# common factor is big-context fragility (long prefill + long-lived TLS
# stream), not one bad network. The SDK already retries connection errors
# internally (max_retries=2, seconds apart); when that fails the provider is
# usually briefly degraded, and a turn-level retry after a longer backoff —
# resuming from the last checkpoint, so tool work is preserved — recovers
# without the user babysitting the error card. Module-level so tests can tune.
AUTO_RETRY_MAX = 2
AUTO_RETRY_BACKOFF_S = (5.0, 20.0)

# Matched by CLASS NAME (not import) so one classifier covers the openai,
# anthropic and httpx stacks without hard imports. Covers: SDK connection
# wrappers (APIConnectionError — what an ssl.SSLError surfaces as), SDK
# timeouts, httpx transport errors (ConnectError/ReadTimeout/RemoteProtocol…),
# bare ssl/socket errors, and the stall watchdog's RuntimeError (checked by
# message). 429/5xx APIStatusErrors are also retried (via status_code below):
# the SDK already exhausted its fast internal retries by the time we see them,
# and a longer backoff is exactly what an overloaded provider needs.
_TRANSIENT_EXC_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "TransportError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "SSLError",
    # NOTE: deliberately NOT bare "TimeoutError" (== asyncio.TimeoutError):
    # tool-internal asyncio.wait_for timeouts would falsely retry the whole
    # turn. Network timeouts arrive as httpx ReadTimeout / openai
    # APITimeoutError / anthropic APITimeoutError — all covered by name.
    "ConnectionError",
    "ConnectionResetError",
    "ChunkedEncodingError",
    "IncompleteRead",
})


def _is_transient_model_error(exc: BaseException) -> bool:
    """True when a turn failure looks like a transient provider/network issue.

    Walks the __cause__/__context__ chain: langgraph surfaces the model error
    directly, but wrappers (RetryError, task groups) can nest the real one.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, RuntimeError) and "model/stream stall" in str(e):
            return True  # CHUNK_TIMEOUT_S watchdog (see chunked_stream)
        if any(cls.__name__ in _TRANSIENT_EXC_NAMES for cls in type(e).__mro__):
            return True
        sc = getattr(e, "status_code", None)
        if isinstance(sc, int) and (sc == 408 or sc == 429 or sc >= 500):
            return True
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None:
            stack.append(e.__context__)
    return False


class TurnStopped(Exception):
    """Raised inside the chunked stream loop when the user stops the turn.

    Cooperative by design (NOT task.cancel): the LLM retry layers swallow
    CancelledError (see the watchdog note below), so the stop signal rides an
    asyncio.Event checked between chunks / inside the per-chunk wait instead.
    """


async def _heal_interrupted_turn(graph, config: dict, seg_text: str = "") -> None:
    """Repair checkpoint state after a turn was stopped mid-superstep.

    Checkpoints commit per superstep, so an interrupted turn can leave:
    1. a trailing AIMessage whose tool_calls have NO ToolMessage answers —
       the next turn would forward that to the provider API and get a 400;
    2. streamed-but-uncommitted assistant text (only exists client-side).

    Fix both via aupdate_state (precedent: compaction.py), using a DERIVED
    turn_id — the original one is in ABANDONED_TURNS and would be refused by
    FileCheckpointer.aput/aput_writes. Heal only when actually needed: a
    needless update on a clean thread still forks a checkpoint. Empirically
    verified (langgraph 1.2.9 + FileCheckpointer delta mode): as_node="agent"
    with pending_tool_calls=[] routes through route_after_agent to END, so
    the repaired checkpoint has next=() and the next invoke starts clean
    (it also clears a parked permission interrupt).
    """
    session_id = (config.get("configurable") or {}).get("thread_id", "")
    try:
        snap = await graph.aget_state(config)
        msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
        # 3. A steered message the agent superstep ABSORBED but never committed
        # (the turn was stopped during the model call reading it). Its band is
        # already on screen — acknowledged at drain time — so commit it, or the
        # user's message would silently vanish from the transcript. Added first:
        # it was absorbed after the tool results and before whatever the
        # never-committed superstep would have produced.
        _inflight = shared.steer_take_inflight(session_id)
        # Same builder the absorbing agent_node uses, so a message committed by
        # the heal is byte-identical to the one the drain would have produced
        # (attachments included: the steered message keeps its multimodal
        # content, and a document steer's companion [turn context] message is
        # reconstructed with it under its own derived id).
        heal: list = shared.steer_messages(_inflight)
        if msgs and isinstance(msgs[-1], AIMessage):
            # Trailing AIMessage ⇒ nothing after it can answer its tool_calls:
            # every one is dangling and needs an "(interrupted)" placeholder.
            last_ai = msgs[-1]
            for tc in getattr(last_ai, "tool_calls", None) or []:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    tc_name = (
                        tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    ) or ""
                    heal.append(
                        ToolMessage(
                            content="(interrupted)",
                            tool_call_id=tc_id,
                            name=tc_name,
                        )
                    )
        elif (seg_text or "").strip():
            # Superstep never committed — persist the streamed partial answer
            # so the next turn's model (and a history reload) can see it.
            agent_id = (config.get("configurable") or {}).get("agent_id") or ""
            heal.append(
                AIMessage(
                    content=seg_text,
                    **({"additional_kwargs": {"agent_id": agent_id}} if agent_id else {}),
                )
            )
        if not heal:
            return
        turn_id = (config.get("configurable") or {}).get("turn_id") or ""
        heal_config = {
            **config,
            "configurable": {
                **(config.get("configurable") or {}),
                "turn_id": f"{turn_id}:stop",
            },
        }
        await graph.aupdate_state(
            heal_config, {"messages": heal, "pending_tool_calls": []}, as_node="agent"
        )
        _log.info(
            "turn_stop_healed session=%s turn=%s msgs=%d", session_id, turn_id, len(heal)
        )
    except Exception:
        # Healing is best-effort; a failure must not break the stop path.
        _log.exception("turn_stop_heal_failed session=%s", session_id)


async def _stop_parked_turn(session: dict, session_id: str, turn_id: str) -> None:
    """Stop a turn parked at a permission/version-propose/ask_user interrupt.

    No live stream task exists — heal the persisted state (the aupdate_state
    also clears the pending interrupt, verified empirically) and broadcast
    turn.stopped so every tab leaves the running state. File IO stays off the
    receive loop: callers run this via spawn_bg.
    """
    try:
        graph = session["graph"]
        config = {
            "configurable": {
                "thread_id": session_id,
                "project_slug": session["project_slug"],
                "turn_id": turn_id,
            }
        }
        # Heal under the turn lock: the paused turn's task may still be
        # draining its stream generator, and its suspension checkpoint is
        # only flushed during that drain. Healing earlier reads the PRE-pause
        # checkpoint and then gets overwritten by the flush (the dangling
        # tool_calls survive). The lock is held until _run_stream returns,
        # i.e. after the drain. Same serialization as _resume_job.
        async with _turn_lock(session_id):
            await _heal_interrupted_turn(graph, config)
        await _push_session_event(session_id, "turn.stopped", {}, turn_id or None)
        # This is the one way a parked turn ends WITHOUT a resume, so it is the
        # one place a parked segment's stash has to be dropped here: the segment
        # itself ended on the not-parked gate's blind side (_stream_graph keeps
        # the stash for a resume that will never come now), and the client sees
        # turn.stopped and re-sends whatever it never saw acknowledged
        # (steering-design §3.3). Without this the entry would sit until the
        # session's next turn and be injected there.
        #
        # A parked SUBAGENT turn the user just stopped is terminal "stopped"
        # (its bg settle hook skipped while the interrupt was parked) — and it
        # must be finalized BEFORE the reconcile below: the reconcile spawns
        # its redelivery wakes first, such a wake would win the freed turn
        # lock and open a ghost turn on the session (fresh stop event,
        # _RUNNING_TURNS re-set), this finalize would then hit the terminal
        # guard too late or the ghost's settle would mark the stopped session
        # done and inject it upward (major-B). Awaited (not spawn_bg) so the
        # ordering against the reconcile's wakes is guaranteed. A main
        # conversation no-ops here, so major-1's redelivery is untouched.
        try:
            from ...subagent_scheduler import on_turn_stopped

            await on_turn_stopped(session_id)
        except Exception:
            _log.exception("subagent_stop_parked_hook_failed session=%s", session_id)
        #
        # The scheduler's tagged injections get no client re-send — reconcile
        # them into wake turns before the generic clear drops them (major-1;
        # wakes aimed at the just-finalized terminal parent are refused).
        try:
            from ...subagent_scheduler import reconcile_stash_injections

            reconcile_stash_injections(session_id)
        except Exception:
            _log.exception("subagent_reconcile_failed session=%s", session_id)
        shared.steer_clear(session_id)
        _log.info("turn_stopped_parked session=%s turn=%s", session_id, turn_id)
    except Exception:
        _log.exception("stop_parked_turn_failed session=%s", session_id)


async def _stream_graph(
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Drive the graph and emit token / tool / permission events."""
    # Arm the ask_user budget for this turn segment. Every segment (the invoke
    # and each resume) funnels through here, and each runs in its own asyncio
    # task whose context dies with it — so no reset is needed and a budget
    # never leaks into an unrelated turn.
    begin_ask_budget()
    # Pre-initialized so the finally-block bookkeeping below can never raise a
    # NameError that masks the original failure.
    saw_interrupt = False
    ws_closed = False
    # Uncommitted text of the CURRENT agent superstep (reset at each agent
    # commit): what a user stop must persist as a partial AIMessage.
    seg_text: list[str] = []
    stop_waiter: Any = None
    session_id = (config.get("configurable") or {}).get("thread_id", "")
    # Pre-init so the except/finally blocks below can never NameError-mask the
    # original failure when the error lands before the try-body assigns them.
    # (The placeholder Event is replaced by the session's shared stop event
    # inside the try; a fresh one simply reads as "no stop requested".)
    _cfg_conf: dict = config.get("configurable") or {}
    turn_id = _cfg_conf.get("turn_id") or ""
    ui_turn_id = turn_id
    stop_evt = asyncio.Event()
    try:
        # Per-turn trace id (from invoke, or fresh on a bare resume). `emit`
        # wraps _ev so EVERY event of this turn carries it — the frontend shows
        # it on the bubble and we log it, so a user-supplied UUID greps the logs.
        #
        # turn_id here is the EXECUTION id: an auto/checkpoint retry of an
        # abandoned turn runs under a derived id ("…--a1"/"…--r<hex>") because
        # the original sits in ABANDONED_TURNS, where FileCheckpointer would
        # silently refuse every checkpoint write of the retry (the retry would
        # run but persist nothing). ui_turn_id is the stable CLIENT-facing id:
        # events, last_error and _RUNNING_TURNS keep carrying it so the
        # frontend sees one continuous turn and its retry button round-trips.
        _cfg_conf = config.get("configurable") or {}
        turn_id = _cfg_conf.get("turn_id") or str(uuid.uuid4())
        _cfg_conf["turn_id"] = turn_id
        ui_turn_id = _cfg_conf.get("_ui_turn_id") or turn_id
        config["configurable"] = _cfg_conf

        def emit(event: str, data: dict) -> str:
            # frame_session is stamped on every turn frame: the client drops any
            # frame whose frame_session isn't the socket's own session, making a
            # cross-session render leak structurally impossible (2026-10-01).
            # Separate key from data["session_id"] (subagent payload semantics).
            return _ev(event, {"frame_session": session_id, **data}, ui_turn_id)

        _ensure_turn_log()  # (re)point the trace file handler at the active home

        slug = (config.get("configurable") or {}).get("project_slug", "default")
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        agent_id = (config.get("configurable") or {}).get("agent_id", "")
        # Usage telemetry: continuation turns are tagged by the goal driver;
        # everything else is user-driven "chat" (usage-stats-design.md §3.6).
        usage_source = (config.get("configurable") or {}).get("usage_source") or "chat"
        _RUNNING_TURNS[session_id] = ui_turn_id
        # Cooperative stop signal: setdefault so an event pre-armed by the WS
        # loop (created before this task spawned) is never clobbered.
        stop_evt = _TURN_STOP.setdefault(session_id, asyncio.Event())

        # The client (app webview / browser tab) can close the socket mid-turn
        # (refresh, navigate, sleep). Turn events therefore broadcast to EVERY
        # live socket of the session instead of only the invoking one: when the
        # invoke socket dies the client reconnects a fresh socket and keeps
        # receiving the running stream (2026-08-05 incident: the turn completed
        # server-side but its second half went nowhere because delivery was
        # tied to the dead socket). safe_send swallows per-socket send errors
        # and prunes dead sockets; ws_closed records whether the most recent
        # attempt found ANY live socket (diagnostic only — never latches, so a
        # reconnect mid-turn resumes delivery).
        ws_closed = False

        async def safe_send(data: str) -> None:
            nonlocal ws_closed
            socks = _SESSION_WS.get(session_id) or []
            alive: list[Any] = []
            for w in socks:
                if await _try_send(w, data):
                    alive.append(w)
            _SESSION_WS[session_id] = alive
            ws_closed = not alive

        async def keepalive() -> None:
            # The WS receive loop is sequential, so it can't answer the client's
            # app-level pings while a turn runs. If a tool/LLM step goes silent
            # for >45s the frontend's watchdog closes the socket (the "stuck at
            # 'now creating doc'" symptom). Send a harmless keepalive frame well
            # under that window so the client's lastSeen keeps resetting — even
            # while no socket is connected, so a reconnecting client never lands
            # in a >45s silent gap during a long tool step.
            try:
                while True:
                    await asyncio.sleep(15)
                    await safe_send(emit("keepalive", {}))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        _ka = asyncio.create_task(keepalive())

        # Snapshot the global TODO list before the turn: afterwards we diff it
        # to find the items the agent created/touched, and auto-link THIS
        # session to them (TODO panel → sessions association, docs "TODO 特性").
        _pre_turn_todos = todo_store.list_todos()

        # Steering ack, injected into config so graph.agent_node can fire it at the
        # exact moment it absorbs (docs/steering-design.md §3.2). Emitting it HERE
        # rather than waiting for the superstep's update is what puts the client's
        # transcript band at the injection point: the band must land right after
        # the tool batch that was running, and the model's continuation tokens
        # stream immediately after the drain. Acking at commit time put the band
        # AFTER that continuation (caught by driving the real UI), and since the
        # replayed view reads the state order, live and replay disagreed.
        # In-flight bookkeeping below makes the ack durable again: it is cleared
        # by the commit, committed by the heal if the turn is stopped first, and
        # re-stashed when a parked exit means the resumed segment will re-drain.
        async def _steer_absorbed(entries: list[dict]) -> None:
            shared.steer_mark_inflight(session_id, entries)
            for _e in entries:
                _log.info(
                    "steer_absorbed session=%s steer=%s turn=%s",
                    session_id,
                    _e.get("steer_id"),
                    turn_id,
                )
                await safe_send(
                    emit(
                        "steer.absorbed",
                        {
                            "steer_id": _e.get("steer_id"),
                            "injected_at": _e.get("injected_at"),
                        },
                    )
                )

        config.setdefault("configurable", {})["steer_absorbed"] = _steer_absorbed
        # Superstep budget — see turn_recursion_limit above.
        config.setdefault("recursion_limit", turn_recursion_limit())

        if command is not None:
            stream = graph.astream(command, config=config, stream_mode=["messages", "updates"])
        else:
            stream = graph.astream(input_state, config=config, stream_mode=["messages", "updates"])
        saw_interrupt = False
        # Recursion-budget pre-warn (subagent sessions only): once the consumed
        # supersteps get within _RECURSION_WARN_MARGIN of the limit, one
        # synthetic steer tells the model to stop calling tools and write its
        # final report — most recursion deaths are then avoided outright; the
        # ones that still hit the limit are salvaged by the scheduler's
        # wrap-up continuation (subagent_scheduler on_turn_settled).
        _r_limit = int(config.get("recursion_limit") or 0)
        _r_warned = False
        _agent_steps = 0
        _found_meta = _find_meta(session_id)
        _is_subagent_session = bool(
            _found_meta and _found_meta[0].get("type") == "subagent"
        )
        special_ids: dict[str, str] = {}  # tool_call id -> special tool name (no bubble)
        tool_args_by_id: dict[str, tuple[str, dict]] = {}  # id -> (name, args)
        # tool_call ids that already emitted a tool.start bubble. Parallel tool
        # calls each carry their own id (and a distinct streaming ``index``);
        # keying on id — not ``index == 0`` — ensures every one of a parallel
        # batch gets its bubble (the old ``not index`` check only surfaced the
        # first, silently dropping the rest from the live view).
        started_tool_ids: set[str] = set()
        # Track tool names by id so we can emit ``tool.args`` from the chunks
        # stream (the first chunk carries the name, subsequent ones don't).
        _tool_name_by_id: dict[str, str] = {}
        # Accumulate tool_call args fragments from streaming chunks so we can
        # emit ``tool.args`` (the command text) as soon as the JSON is complete
        # — before the tool actually runs. Without this, ``tool.args`` only
        # fires in ``updates`` mode (after the agent node finishes), which for
        # some models arrives too late (or simultaneously with ``tool.end``).
        _partial_tool_args: dict[str, str] = {}
        _emitted_tool_args: set[str] = set()
        turn_text: list[str] = []  # accumulate assistant text for memory capture
        # Fresh turn (not a permission resume): announce the resolved agent so the
        # UI can label the assistant bubble authoritatively (never the generic
        # "Agent" fallback).
        if command is None:
            # A new attempt supersedes any persisted last_error (empty dict =
            # cleared; _session_meta_patch skips None values).
            _session_meta_patch(slug, session_id, {"last_error": {}})
            _aid = (config.get("configurable") or {}).get("agent_id")
            _ag = agents_reg.get_agent(_aid) if _aid else None
            _log.info(
                "turn_start session=%s turn=%s agent=%s text=%r",
                session_id, turn_id, _aid,
                ((config.get("configurable") or {}).get("user_text") or "")[:120],
            )
            await safe_send(
                emit("turn.start", {"turn_id": ui_turn_id, "agent_id": _aid or "", "name": _ag.name if _ag else "Agent"})
            )
        else:
            _log.info("turn_resume session=%s turn=%s agent=%s", session_id, turn_id, agent_id)

        # Wall-clock stall watchdog: the SDK httpx read-timeout only covers
        # *network* reads, NOT a stall inside the model generator or graph (the
        # 7m49s "stuck at 'now creating doc'" case). Wrap the stream iterator so
        # any chunk that takes longer than CHUNK_TIMEOUT_S ends the stream ->
        # the except below surfaces a fast `error` event instead of hanging.
        # A legitimately long tool call is fine because its tool-result
        # `updates` chunk resets the per-chunk clock.
        # NOTE: asyncio.wait_for is NOT used here — cancellation of the stuck
        # __anext__ can be swallowed by retry layers (they catch CancelledError
        # and keep retrying), which makes wait_for wait forever and the
        # watchdog never fires. Instead await with asyncio.wait and ABANDON the
        # stuck task on timeout (fire-and-forget cancel); at most one stuck
        # task leaks per stall, and the turn still errors out fast.
        # One waiter per turn on the cooperative stop signal (armed by the WS
        # `stop` handler); cancelled in the finally block. Sharing the
        # per-chunk asyncio.wait below means a stop lands promptly even while
        # a long tool/LLM step holds __anext__ — no task.cancel() needed.
        stop_waiter = asyncio.ensure_future(stop_evt.wait())

        async def chunked_stream():
            it = stream.__aiter__()
            while True:
                nxt = asyncio.ensure_future(it.__anext__())
                nxt.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )  # mark result retrieved, silence "never retrieved" warnings
                done, _ = await asyncio.wait(
                    {nxt, stop_waiter},
                    timeout=CHUNK_TIMEOUT_S,
                    # FIRST_COMPLETED, not the ALL_COMPLETED default: the
                    # stop waiter stays pending until a stop lands, and with
                    # ALL_COMPLETED every chunk would block to the timeout.
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    nxt.cancel()
                    # Same abandonment protocol as the stall watchdog below:
                    # block the detached run's late checkpoint writes, then
                    # unwind through the dedicated TurnStopped handler (state
                    # heal + turn.stopped event). Cooperative event, NOT
                    # task.cancel — retry layers swallow CancelledError.
                    ABANDONED_TURNS.add(turn_id)
                    raise TurnStopped()
                if not done:
                    nxt.cancel()
                    # Block any late checkpoint writes from this detached run
                    # (see ABANDONED_TURNS) so it can't roll back a retry.
                    ABANDONED_TURNS.add(turn_id)
                    raise RuntimeError(
                        f"model/stream stall: no chunk for {CHUNK_TIMEOUT_S:.0f}s"
                    )
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return

        async for mode, payload in chunked_stream():
            if mode == "messages":
                chunk, msg_meta = payload
                # Only AI message chunks carry streaming text / thinking /
                # tool-call chunks. ToolMessage chunks (the result emitted by
                # the tools node, type "tool") must NOT be streamed as
                # token.delta — that would leak the tool output into the
                # assistant's text bubble (and into the memory-capture buffer).
                # Tool results reach the UI via the `updates` mode -> tool.end.
                # Streamed AI chunks report type "AIMessageChunk"; a final
                # non-streamed AIMessage reports "ai" — allow both.
                if getattr(chunk, "type", None) not in ("ai", "AIMessageChunk"):
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    for b in content:
                        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        if btype == "thinking":
                            txt = b.get("thinking") or b.get("text") or ""
                            if txt:
                                await safe_send(emit("thinking.delta", {"content": txt}))
                        elif btype == "text":
                            txt = b.get("text") or ""
                            if txt:
                                turn_text.append(txt)
                                seg_text.append(txt)
                                await safe_send(emit("token.delta", {"content": txt}))
                elif isinstance(content, str) and content:
                    turn_text.append(content)
                    seg_text.append(content)
                    await safe_send(emit("token.delta", {"content": content}))
                rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if rk:
                    await safe_send(emit("thinking.delta", {"content": rk}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        tc_id = tc.get("id")
                        tc_index = tc.get("index")
                        tc_name = tc.get("name")
                        # Fire one tool.start per distinct tool_call id (the
                        # chunk that carries the name). See started_tool_ids —
                        # keying on id lets parallel tool calls each surface.
                        if tc_name and tc_id and tc_id not in started_tool_ids:
                            started_tool_ids.add(tc_id)
                            _tool_name_by_id[tc_id] = tc_name
                            if (
                                tc_name in RENDER_TOOL_NAMES
                                or tc_name in WORKFLOW_TOOL_NAMES
                                or tc_name in ARTIFACT_TOOL_NAMES
                            ):
                                special_ids[tc_id] = tc_name
                                continue  # surfaced as widget/ref/workflow block, not a tool bubble
                            await safe_send(
                                emit("tool.start", {"name": tc_name, "id": tc_id})
                            )
                        # Accumulate args fragments from streaming chunks.
                        # Streaming chunks only carry ``id`` on the first
                        # fragment; subsequent ones use ``index``. Resolve
                        # the id by matching the index to the most recent
                        # tool call that hasn't yet received complete args.
                        if not tc_id and tc_index is not None:
                            for sid in reversed(list(started_tool_ids)):
                                if sid not in _emitted_tool_args:
                                    tc_id = sid
                                    break
                        args_frag = tc.get("args") or ""
                        if tc_id and args_frag and tc_id not in _emitted_tool_args:
                            _partial_tool_args[tc_id] = _partial_tool_args.get(tc_id, "") + args_frag
                            raw = _partial_tool_args[tc_id]
                            try:
                                import json as _json
                                parsed = _json.loads(raw)
                                if isinstance(parsed, dict):
                                    nm = _tool_name_by_id.get(tc_id, "")
                                    preview = _tool_args_preview(nm, parsed)
                                    if preview:
                                        _emitted_tool_args.add(tc_id)
                                        await safe_send(
                                            emit("tool.args", {"id": tc_id, "preview": preview})
                                        )
                            except (ValueError, TypeError):
                                pass  # incomplete JSON, keep accumulating
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "agent":
                        # Recursion pre-warn: agent supersteps ≈ half the
                        # budget (each round is agent + tools), warn once when
                        # the remainder narrows to the margin.
                        _agent_steps += 1
                        if (
                            _is_subagent_session
                            and _r_limit
                            and not _r_warned
                            and 2 * _agent_steps >= _r_limit - 10
                        ):
                            _r_warned = True
                            shared.steer_enqueue(
                                session_id,
                                {
                                    "steer_id": f"recursion-warn-{turn_id}",
                                    # Model-facing steering (i18n 分流规则):
                                    # inline bilingual t(), never a catalog key.
                                    "text": t(
                                        "[System reminder] This turn is about to "
                                        f"hit the step limit (~{2 * _agent_steps}/"
                                        f"{_r_limit} steps used). Stop issuing new "
                                        "tool calls and immediately write your "
                                        "final report from the material gathered "
                                        "so far.",
                                        "【系统提醒】本回合即将达到步数上限"
                                        f"（已用约 {2 * _agent_steps}/{_r_limit} 步）。"
                                        "请停止发起新的工具调用，基于已有材料"
                                        "立即输出最终报告。",
                                    ),
                                },
                            )
                        # Superstep COMMIT point: everything streamed so far is
                        # checkpointed, so the uncommitted segment restarts here
                        # (a user stop only persists what's still uncommitted).
                        seg_text.clear()
                        for m in (delta or {}).get("messages", []):
                            # A steered message COMMITTED with this superstep
                            # (docs/steering-design.md §3.2): its ack was already
                            # sent at drain time (see _steer_absorbed), so all
                            # that is left is to retire the uncommitted-drain
                            # record — from here on the message is in the
                            # checkpoint, and the heal must not touch it again.
                            if (getattr(m, "additional_kwargs", None) or {}).get(
                                "ginno_steer"
                            ):
                                shared.steer_clear_inflight(session_id)
                            # D2 — per-call usage + session accumulator. Only
                            # complete AIMessages carry usage_metadata (never
                            # streamed chunks), so this fires once per LLM call.
                            u = extract_usage(m)
                            if u:
                                acc = _USAGE_BY_SESSION.setdefault(session_id, empty_usage())
                                add_usage(acc, u)
                                # Persist the call into the global usage log
                                # (usage-stats-design.md §4). Best-effort: the
                                # store never raises, so this cannot break the
                                # turn. provider/model come from the session's
                                # meta (resolved at create/rebuild time).
                                _sreg = _SESSIONS.get(session_id) or {}
                                usage_store.record(
                                    input_tokens=u["input_tokens"],
                                    output_tokens=u["output_tokens"],
                                    cache_read_tokens=u["cache_read_tokens"],
                                    cache_creation_tokens=u["cache_creation_tokens"],
                                    provider=_sreg.get("model_provider") or "",
                                    model=_sreg.get("model_name") or "",
                                    source=usage_source,
                                    session_id=session_id or None,
                                    project_slug=slug or None,
                                    agent_id=agent_id or None,
                                    turn_id=turn_id,
                                )
                                await safe_send(
                                    emit(
                                        "usage",
                                        {
                                            "turn": u,
                                            "session": dict(acc),
                                            "cache_hit_ratio": cache_hit_ratio(acc),
                                        },
                                    )
                                )
                            for tc in getattr(m, "tool_calls", []) or []:
                                nm = tc.get("name")
                                args = tc.get("args") or {}
                                tc_id = tc.get("id")
                                tool_args_by_id[tc_id] = (nm, args)
                                if (
                                    nm in RENDER_TOOL_NAMES
                                    or nm in WORKFLOW_TOOL_NAMES
                                    or nm in ARTIFACT_TOOL_NAMES
                                ):
                                    special_ids[tc_id] = nm
                                elif tc_id:
                                    # Show WHAT is running: surface the tool call's
                                    # args (e.g. the bash command) on the pending
                                    # tool bubble. Fires from the agent update —
                                    # after args are complete, before the tool runs.
                                    # Skip if already emitted from the chunks stream
                                    # (streaming args accumulation above).
                                    if tc_id not in _emitted_tool_args:
                                        preview = _tool_args_preview(nm, args)
                                        if preview:
                                            await safe_send(
                                                emit("tool.args", {"id": tc_id, "preview": preview})
                                            )
                                if nm == "render_widget":
                                    await safe_send(
                                        emit("widget.emit", widget_event(args, tc_id))
                                    )
                                elif nm == "attach_ref":
                                    kind = args.get("kind", "file")
                                    ref_id = args.get("ref_id", "")
                                    if kind == "file":
                                        # Models echo write_file's relative path;
                                        # pin it to the session workspace so
                                        # exists/preview/injection resolve later.
                                        ref_id = _normalize_file_ref(slug, session_id, ref_id)
                                    await safe_send(
                                        emit("ref.emit", {
                                            "kind": kind,
                                            "name": args.get("name", ""),
                                            "ref_id": ref_id,
                                        })
                                    )
                                    if kind in ("file", "doc", "workflow", "link"):
                                        art = art_store.add_artifact(
                                            slug, kind, args.get("name", ""), ref_id,
                                            session_id,
                                        )
                                        if kind == "file":
                                            _register_artifact_file(slug, session_id, art, ref_id)
                                elif nm == "artifact_register":
                                    kind = args.get("kind", "file")
                                    ref = args.get("ref", "")
                                    if kind == "file":
                                        ref = _normalize_file_ref(slug, session_id, ref)
                                    art = art_store.add_artifact(
                                        slug,
                                        kind,
                                        args.get("name", ""),
                                        ref,
                                        session_id,
                                    )
                                    if kind == "file":
                                        _register_artifact_file(slug, session_id, art, ref)
                    elif node_name == "__interrupt__":
                        items = delta if isinstance(delta, (list, tuple)) else [delta]
                        for intr in items:
                            value = getattr(intr, "value", None) or intr
                            if isinstance(value, dict) and value.get("kind") == "permission_request":
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "permission_request"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("permission.request", {
                                        "tool": value.get("tool"),
                                        "args": value.get("args"),
                                    })
                                )
                            elif isinstance(value, dict) and value.get("kind") == "version_propose":
                                # P5: workflow edit awaiting diff confirmation.
                                # Independent of the permission system; resumed via
                                # the same permission_response WS message.
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "version_propose"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("version.propose", {
                                        "workflow_id": value.get("workflow_id"),
                                        "from_version": value.get("from_version"),
                                        "diff": value.get("diff", ""),
                                        "rationale": value.get("rationale", ""),
                                    })
                                )
                            elif isinstance(value, dict) and value.get("kind") == "user_question":
                                # ask_user parked the turn on an ambiguity. The
                                # card is a transcript block (not a ref like the
                                # permission prompt), so this event carries the
                                # tool_call id the client merges on.
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "user_question"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("user.question", {
                                        "id": value.get("id"),
                                        "question": value.get("question", ""),
                                        "header": value.get("header", ""),
                                        "options": value.get("options") or [],
                                        "allow_free_text": value.get("allow_free_text", True),
                                    })
                                )
                    elif node_name == "tools":
                        msgs = (delta or {}).get("messages", [])
                        for m in msgs:
                            tc_id = getattr(m, "tool_call_id", None)
                            raw = getattr(m, "content", "") or ""
                            nm = special_ids.get(tc_id)
                            if nm in WORKFLOW_TOOL_NAMES:
                                mm = re.search(r"run_id=([0-9a-f]{6,})", raw)
                                rid = mm.group(1) if mm else None
                                run = (RUN_CACHE.get(rid) if rid else None) or (
                                    wf_store.get_run(rid) if rid else None
                                )
                                if run:
                                    # 唤起 in-session (design A): bind the chat-triggered
                                    # run to this session and drive it with the real engine.
                                    if not run.get("present_in_session_id"):
                                        run["session_id"] = session_id
                                        run["present_in_session_id"] = session_id
                                        run["updated"] = time.time()
                                        wf_storemod._write_json(wf_storemod._run_path(run["id"]), run)
                                    await safe_send(
                                        emit("run.bind", {
                                            "run_id": run["id"],
                                            "workflow_id": run["workflow_id"],
                                            "present_in_session_id": session_id,
                                        })
                                    )
                                    _wf_task = _WF_RUN_TASKS.get(run["id"])
                                    if (_wf_task is None or _wf_task.done()) and run.get("status") == "running":
                                        _spawn_run_task(
                                            run["id"],
                                            _run_workflow_bg(
                                                run["id"],
                                                run["workflow_id"],
                                                run.get("context_override"),
                                                session_id,
                                            ),
                                        )
                                    await safe_send(emit("workflow.emit", {"run": run}))
                                # no ordinary tool bubble for workflow tools
                            elif nm in RENDER_TOOL_NAMES or nm in ARTIFACT_TOOL_NAMES:
                                pass  # widget/ref emitted at agent-update; no bubble
                            elif tc_id:
                                await safe_send(
                                    emit("tool.end", {"id": tc_id, "content": _truncate_for_ws(_tool_content_str(raw))})
                                )
                                # File-reactive side effects (docs §7.5): invalidate
                                # previews of files this tool touched; auto-register
                                # + open derived analysis results.
                                await _tool_file_effects(
                                    safe_send, emit, slug, session_id,
                                    tool_args_by_id.get(tc_id), raw,
                                )
                                # Code-panel S3: agent file writes/edits →
                                # ``code.changed`` (parsed from the marker
                                # write_file / edit_file append to their own
                                # result; there is no Hook for this — its
                                # PostToolUse is declared but never dispatched).
                                await _emit_code_changes(safe_send, emit, slug, session_id, raw)
                    elif node_name == "permission":
                        # resolve "running" tool bubbles that were denied by
                        # tools_allow / hooks / policy / user (not streamed)
                        for m in (delta or {}).get("messages", []):
                            c = getattr(m, "content", "")
                            if isinstance(c, str) and c.startswith(BLOCK_PREFIX):
                                rest = c[len(BLOCK_PREFIX):]
                                name, _, reason = rest.partition("]")
                                await safe_send(
                                    emit("tool.end", {
                                        "name": name.strip(),
                                        "content": reason.strip() or c,
                                    })
                                )
        # Auto-associate this session with every TODO the turn created or
        # touched — the TODO panel surfaces these sessions as jump targets.
        # Gated on an actual todo_* tool call so unrelated edits don't count.
        if session_id and any(
            (nm or "").startswith("todo_") for nm, _args in tool_args_by_id.values()
        ):
            for t in todo_store.touched_since(_pre_turn_todos):
                if t.get("id") and session_id not in (t.get("session_ids") or []):
                    todo_store.link_session(t["id"], session_id)
        # refresh the right-panel TODO list after every turn (the agent may
        # have mutated it via the todo_* tools); the checkbox path is optimistic
        # and doesn't need this.
        await safe_send(emit("todos.changed", {}))
        await safe_send(emit("workflows.changed", {}))
        await safe_send(emit("artifacts.changed", {}))
        # skills too: a turn may have installed/uninstalled skills (the
        # install_skills tool — or even bash), and the slash menu / next
        # turn's skills index must reflect it without a manual refresh.
        await safe_send(emit("skills.changed", {}))
        if not saw_interrupt:
            _final_text = "".join(turn_text)
            # Citation framework: parse/validate the trailing block against the
            # turn's registered sources, record the usage ledger. Runs before
            # memory capture so the block can be stripped from the pool text.
            _verified = 0
            try:
                _verified = await _process_turn_citations(session_id, turn_id, _final_text)
            except Exception:
                _log.exception("citations_failed session=%s turn=%s", session_id, turn_id)
            # Capture sanitized assistant text for memory summarization (P2).
            # Also reused as the desktop notification body below (the web shell
            # shows a turn-done notification when the user looked away).
            # Gate 1: `capture` config (revived) + verified-citation quality
            # signal travels with the entry for evidence-weighted distilling.
            from ...knowledge.citations import strip_citation_block

            _clean_text = strip_citation_block(_final_text)
            if turn_text:
                from ...knowledge.config import load_knowledge_config
                from ...memory import append_to_pool

                _mem_cfg = load_knowledge_config()
                if _mem_cfg.capture:
                    append_to_pool(session_id, agent_id, _clean_text, cited=_verified > 0)
                    _maybe_auto_distill(_mem_cfg)
            # LLM subject title (title_gen): armed by _touch_session_title on
            # the first invoke, fired by the first turn that completes without
            # an interrupt. Background + best-effort — failure keeps the
            # truncated placeholder and retries on the next completed turn.
            if _clean_text.strip():
                from ...title_gen import spawn_title_gen

                _found = _find_meta(session_id)
                if _found and _found[0].get("title_llm_pending"):
                    _session_meta_patch(
                        slug, session_id, {"title_assistant": _clean_text[:2000]}
                    )
                    spawn_title_gen(session_id, turn_id)
            _log.info(
                "turn_done session=%s turn=%s status=completed text_len=%d",
                session_id, turn_id, len("".join(turn_text)),
            )
            # Empty text (tool-only turn) → the UI falls back to a generic body.
            await safe_send(emit("message.end", {"text": _clean_text.strip()[:200]}))
        else:
            _log.info(
                "turn_done session=%s turn=%s status=paused_at_interrupt",
                session_id, turn_id,
            )
    except TurnStopped:
        # User pressed stop: heal the persisted state (dangling tool_calls /
        # uncommitted partial text), tell the clients, exit quietly. No error
        # card, no last_error, no completion side effects (memory/title/
        # citations live under the normal-completion branch below).
        await _heal_interrupted_turn(graph, config, "".join(seg_text))
        _log.info(
            "turn_stopped session=%s turn=%s seg_len=%d",
            session_id, turn_id, len("".join(seg_text)),
        )
        await safe_send(emit("turn.stopped", {}))
    except Exception as e:
        # Transient provider/network failure (SSL drop, connection error,
        # 429/5xx, stall watchdog): auto-retry from the latest checkpoint with
        # a backoff instead of failing the turn — see AUTO_RETRY_MAX notes.
        # The retry runs under a DERIVED exec turn_id (the abandoned original
        # would have its checkpoint writes refused) while every client-facing
        # surface keeps ui_turn_id. The caller still holds _turn_lock, so the
        # recursive call must NOT re-acquire it (it doesn't — the lock lives in
        # the job wrappers). A user stop during the backoff aborts the retry.
        if not stop_evt.is_set() and _is_transient_model_error(e):
            _attempt = int(_cfg_conf.get("_auto_retry_attempt") or 0)
            if _attempt < AUTO_RETRY_MAX:
                _backoff = AUTO_RETRY_BACKOFF_S[
                    min(_attempt, len(AUTO_RETRY_BACKOFF_S) - 1)
                ]
                _log.warning(
                    "turn_auto_retry session=%s turn=%s attempt=%d/%d backoff=%.0fs err=%s: %s",
                    session_id, turn_id, _attempt + 1, AUTO_RETRY_MAX,
                    _backoff, type(e).__name__, str(e)[:200],
                )
                await safe_send(
                    emit(
                        "notice",
                        {
                            # Event contract (i18n-design.md §3): English
                            # fallback text + i18n_key + params for the UI.
                            "message": (
                                f"Model connection error ({type(e).__name__}); "
                                f"auto-retrying in {_backoff:.0f}s "
                                f"({_attempt + 1}/{AUTO_RETRY_MAX})…"
                            ),
                            "i18n_key": "stream.auto_retry",
                            "params": {
                                "error": type(e).__name__,
                                "seconds": f"{_backoff:.0f}",
                                "attempt": _attempt + 1,
                                "max": AUTO_RETRY_MAX,
                            },
                        },
                    )
                )
                await asyncio.sleep(_backoff)
                if not stop_evt.is_set():
                    _retry_conf = {
                        **config,
                        "configurable": {
                            **_cfg_conf,
                            # Random suffix: a later retry chain of the same
                            # ui turn must never collide with an earlier
                            # chain's abandoned exec id (exact-match gate in
                            # FileCheckpointer.aput/aput_writes).
                            "turn_id": f"{ui_turn_id}--a{_attempt + 1}-{uuid.uuid4().hex[:6]}",
                            "_ui_turn_id": ui_turn_id,
                            "_auto_retry_attempt": _attempt + 1,
                        },
                    }
                    # Resume from the latest checkpoint when one exists (tool
                    # work preserved). A failure on the FIRST model call of a
                    # fresh session commits nothing — resuming with None would
                    # die on EmptyInputError — so re-run the original input
                    # instead. Neither available: fall through to the error.
                    try:
                        _snap = await graph.aget_state(config)
                        _has_ckpt = bool(getattr(_snap, "values", None) or {})
                    except Exception:
                        _has_ckpt = False
                    if _has_ckpt or input_state is not None:
                        await _stream_graph(
                            graph,
                            _retry_conf,
                            input_state=None if _has_ckpt else input_state,
                            command=None,
                        )
                        return
        _log.exception("turn_error session=%s turn=%s", session_id, turn_id)
        # Event contract (i18n-design.md §3): the raw exception dump (+ remedy
        # hint for known failures) stays the English fallback; the key lets the
        # UI render a localized version.
        _err_fields = turn_error_fields(e)
        # Persist the failure on the session meta so the error card (with its
        # retry action) survives webview reloads and route/session switches —
        # the history endpoint re-surfaces it as the last message. The retry
        # button round-trips ui_turn_id; the handler derives a fresh exec id
        # when this turn was abandoned.
        _session_meta_patch(
            slug,
            session_id,
            {
                "last_error": {
                    "turn_id": ui_turn_id,
                    "message": _err_fields["message"],
                    "at": time.time(),
                }
            },
        )
        await safe_send(
            emit(
                "error",
                {
                    **_err_fields,
                },
            )
        )
    finally:
        try:
            _ka.cancel()
        except Exception:
            pass
        if stop_waiter is not None and not stop_waiter.done():
            stop_waiter.cancel()
        # major-B: capture BEFORE the pop — a set event here means the USER
        # stopped this turn (the TurnStopped unwind, or a stop landing during
        # retry backoff). The reconcile below must not spawn result-redelivery
        # wakes ahead of the stopped finalize on such a turn.
        _user_stopped = stop_evt.is_set()
        # A parked-at-interrupt turn no longer needs the stop event either —
        # the WS `stop` handler heals parked turns directly (no live stream).
        _TURN_STOP.pop(session_id, None)
        # Ended or errored (not paused at an interrupt): unregister so a client
        # `turn_state` query reports "not running". A turn parked at a
        # permission/version-propose/ask_user interrupt stays registered — its
        # resume hasn't happened yet.
        if not saw_interrupt:
            _RUNNING_TURNS.pop(session_id, None)
            _PENDING_RESUME.discard(session_id)
            _PENDING_KIND.pop(session_id, None)
            # Steering entries live exactly one turn segment (steering-design
            # §3.2, §6). Clearing on the SAME gate as the unregistering above is
            # load-bearing: the client sends `steer` and the resume
            # back-to-back, and that steer can land while this segment is still
            # draining its generator — i.e. before this line runs. A parked exit
            # must therefore KEEP the stash for the resumed segment to absorb
            # (e2e test_steer_at_a_permission_card_redirects_the_turn catches the
            # version that cleared here unconditionally). Dropping an unabsorbed
            # entry is safe because the client re-sends it (design §3.3).
            #
            # EXCEPT the scheduler's own injections (steer entries tagged
            # ``ginno_subagent_result``): nothing ever re-sends those, so one
            # landing after this turn's LAST agent superstep drain would be
            # lost. Hand them back to the scheduler for a wake-turn redelivery
            # first (major-1 reconciliation; the settle hook re-checks for
            # entries a raced wake restashed).
            # EXCEPT on a user-stopped turn (major-B): a redelivery wake spawned
            # by the plain reconcile wins the just-freed turn lock AHEAD of this
            # job's settle hook — it opens a GHOST turn (fresh stop event,
            # _RUNNING_TURNS re-set), the settle hook then bounces off the
            # _RUNNING_TURNS guard, the stopped finalize never runs, and the
            # ghost turn's settle marks the stopped session done and injects it
            # upward. Finalize the stopped subagent FIRST; the reconcile below
            # then redelivers only what can still absorb it (a now-terminal
            # parent refuses its wakes; a main conversation keeps the major-1
            # redelivery untouched).
            if _user_stopped:
                try:
                    from ...subagent_scheduler import on_turn_stopped

                    await on_turn_stopped(session_id)
                except Exception:
                    _log.exception(
                        "subagent_stop_finalize_failed session=%s", session_id
                    )
            try:
                from ...subagent_scheduler import reconcile_stash_injections

                reconcile_stash_injections(session_id)
            except Exception:
                _log.exception("subagent_reconcile_failed session=%s", session_id)
            shared.steer_clear(session_id)
            # The uncommitted-drain record is either retired by the commit or
            # committed by the heal (both ran above) — never left behind.
            shared.steer_clear_inflight(session_id)
        else:
            # Parked: the interrupt fires after the agent commit, so a pending
            # drain should not exist — but if one ever did, the resumed segment
            # re-runs the node that drains, so hand it back rather than dropping
            # the user's message on the floor.
            shared.steer_restash_inflight(session_id)
        if ws_closed:
            _log.info(
                "turn_client_gone session=%s turn=%s (no live client socket at the "
                "last send; turn completed server-side)",
                session_id,
                turn_id,
            )
