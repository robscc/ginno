"""Session WebSocket + the per-turn streaming engine.

The WS endpoint accepts invoke / permission_response / turn_state / ping
messages; the streaming engine drives the LangGraph agent loop and broadcasts
token / tool / permission / usage events to every live socket of the session.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from .. import agents as agents_reg
from .. import artifacts as art_store
from .. import commands as _commands
from .. import files as files_mod
from .. import paths, usage_store
from .. import server_shared as shared
from .. import workflows as wf_store
from ..checkpointer import ABANDONED_TURNS
from ..graph import BLOCK_PREFIX, build_all_tools, build_graph, build_turn_context
from ..server_shared import (
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _SESSIONS,
    _USAGE_BY_SESSION,
    _WF_RUN_TASKS,
    _ensure_turn_log,
    _ev,
    _log,
    _push_session_event,
    _try_send,
    _turn_lock,
    spawn_bg,
)
from ..session_meta import _find_meta, _session_meta_patch
from ..todos import store as todo_store
from ..tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ..tools.render_tools import RENDER_TOOL_NAMES
from ..tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES
from ..usage import add_usage, cache_hit_ratio, empty_usage, extract_usage
from ..workflows import store as wf_storemod
from ..world_state import TURN_CONTEXT_PREFIX, SessionCtx, context_settings, sync_world_state
from .files import (
    _compact_schema,
    _heal_workspace_ref,
    _normalize_file_ref,
    _register_artifact_file,
    _session_workspace,
)
from .messages_ui import _tool_args_preview, _tool_content_str, _truncate_for_ws
from .sessions import _ensure_session, _first_agent_id, _start_goal_driver
from .workflows import _run_workflow_bg, _spawn_run_task

router = APIRouter()


async def _touch_session_title(
    slug: str, session_id: str, session: dict, user_text: str, turn_id: str
) -> None:
    """Auto-title a session from its first user message; touch `updated`.

    While meta `title_auto` is set, the first non-empty user message becomes
    the title (single line, 40-char preview — same convention as goal-session
    titles) and a `session_title` event refreshes connected clients so the
    sidebar/TopBar rename live. Every turn — first or not — runs a (possibly
    empty) meta patch, which bumps `updated`; that timestamp is what the
    sidebar's day grouping sorts on.
    """
    found = _find_meta(session_id)
    meta = found[0] if found else {}
    text = (user_text or "").strip()
    if meta.get("title_auto", True) and text:
        title = text.replace("\n", " ")[:40]
        _session_meta_patch(slug, session_id, {"title": title, "title_auto": False})
        session["title_auto"] = False
        await _push_session_event(session_id, "session_title", {"title": title}, turn_id)
    else:
        _session_meta_patch(slug, session_id, {})


@router.websocket("/api/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    session = _ensure_session(session_id)
    if not session:
        await ws.accept()
        await ws.send_text(_ev("error", {"message": f"unknown session: {session_id}"}))
        await ws.close()
        return

    await ws.accept()
    _SESSION_WS.setdefault(session_id, []).append(ws)
    _log.info("ws_open session=%s agent=%s", session_id, session.get("agent_id"))
    graph = session["graph"]
    config = {"configurable": {"thread_id": session_id, "project_slug": session["project_slug"]}}

    # File watcher (docs §7.5): stat the session's registered files every 5s.
    # A changed mtime → preview.invalidate (the UI refreshes if that file is
    # open) + a stale badge on the artifact (cleared on next preview fetch).
    # Sends share _ws_lock so watcher frames never interleave with turn frames.
    _ws_lock = asyncio.Lock()
    _watch_stop = asyncio.Event()

    async def _file_watcher() -> None:
        reg = files_mod.get_registry(session["project_slug"])
        while not _watch_stop.is_set():
            try:
                await asyncio.wait_for(_watch_stop.wait(), timeout=5)
                break  # stop set
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            artifacts_dirty = False
            try:
                for e in reg.list_session(session_id):
                    try:
                        m = Path(e["path"]).stat().st_mtime
                    except OSError:
                        continue
                    if m != e.get("mtime", 0):
                        e["mtime"] = m
                        if not e.get("stale"):
                            reg.mark_stale(e["id"], True)
                            artifacts_dirty = True
                        async with _ws_lock:
                            await ws.send_text(
                                _ev(
                                    "preview.invalidate",
                                    {"file_id": e["id"], "reason": "mtime"},
                                )
                            )
                if artifacts_dirty:
                    async with _ws_lock:
                        await ws.send_text(_ev("artifacts.changed", {}))
            except Exception:
                continue

    _watcher_task = asyncio.create_task(_file_watcher())

    # Re-emit any permission interrupt left pending from a previous connection:
    # the graph pauses at permission_node awaiting a resume, so a reconnect or a
    # session switch mid-permission would otherwise orphan the turn (the prompt
    # is gone and there is no way to resume). The payload mirrors the
    # __interrupt__ handling in _run_stream below, so the client's existing
    # permission.request handler applies unchanged.
    try:
        snap = await graph.aget_state(config)
        for task in getattr(snap, "tasks", []) or []:
            for intr in getattr(task, "interrupts", []) or []:
                value = getattr(intr, "value", None) or intr
                if isinstance(value, dict) and value.get("kind") == "permission_request":
                    # Re-arm the resume guard: after a runtime restart the
                    # in-memory flag is gone even though the interrupt persists.
                    _PENDING_RESUME.add(session_id)
                    _RUNNING_TURNS.setdefault(session_id, "")
                    await ws.send_text(
                        _ev(
                            "permission.request",
                            {"tool": value.get("tool"), "args": value.get("args")},
                        )
                    )
                elif isinstance(value, dict) and value.get("kind") == "version_propose":
                    # a workflow-dev diff proposal pending at disconnect: re-show it
                    # so the turn isn't orphaned (same resume channel as permission).
                    _PENDING_RESUME.add(session_id)
                    _RUNNING_TURNS.setdefault(session_id, "")
                    await ws.send_text(
                        _ev(
                            "version.propose",
                            {
                                "workflow_id": value.get("workflow_id"),
                                "from_version": value.get("from_version"),
                                "diff": value.get("diff"),
                                "rationale": value.get("rationale"),
                            },
                        )
                    )
    except Exception:
        # introspecting resume state must never stop the socket from opening
        pass

    # Cross-restart goal resume (design §4.3.3): an active goal re-arms its
    # continuation driver as soon as the session is loaded again.
    _start_goal_driver(session_id)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(_ev("error", {"message": "invalid JSON"}))
                continue

            kind = msg.get("type")
            if kind == "invoke":
                try:
                    user_text = msg.get("message", "")
                    # Per-turn trace id: prefer the client-supplied one (so the UUID
                    # shown on the user bubble matches the one we log + put on every
                    # event), else mint one. Forward it via config so _stream_graph
                    # tags every emitted event + log line with it.
                    turn_id = msg.get("turn_id") or str(uuid.uuid4())
                    _imgs = msg.get("images") or []
                    _log.info(
                        "invoke session=%s turn=%s agent=%s imgs=%d text=%r",
                        session_id,
                        turn_id,
                        msg.get("agent_id") or session.get("agent_id"),
                        len([i for i in _imgs if isinstance(i, dict) and i.get("data")]),
                        (user_text or "")[:120],
                    )
                    # Slash commands + @mentions → TurnPlan (docs §commands).
                    plan = _commands.resolve_turn(msg, session)
                    if plan.builtin_reply is not None:
                        # Built-in command: reply directly, no graph turn, no agent
                        # persistence, no checkpoint write (ephemeral by design).
                        await ws.send_text(
                            _ev("notice", {"message": plan.builtin_reply}, turn_id)
                        )
                        await ws.send_text(_ev("message.end", {}, turn_id))
                        continue
                    user_text = plan.text
                    await _touch_session_title(
                        session["project_slug"], session_id, session, user_text, turn_id
                    )
                    turn_agent = (
                        plan.agent_override
                        or msg.get("agent_id")
                        or session.get("agent_id")
                        or _first_agent_id()
                    )
                    if turn_agent != session.get("agent_id"):
                        session["agent_id"] = turn_agent
                        _session_meta_patch(
                            session["project_slug"], session_id, {"agent_id": turn_agent}
                        )
                    turn_config = {
                        **config,
                        "configurable": {
                            **config["configurable"],
                            "agent_id": turn_agent,
                            "turn_id": turn_id,
                            "user_text": user_text or "",
                        },
                    }
                    async with _turn_lock(session_id):
                        await _run_stream(
                            ws,
                            graph,
                            turn_config,
                            user_text,
                            session,
                            turn_agent,
                            images=msg.get("images"),
                            files=(msg.get("files") or []) + plan.files_extra,
                            mention_context=plan.mention_ctx,
                            skill_name=plan.skill_name,
                        )
                    # The turn may have created/resumed a goal (goal tools) —
                    # (re)arm the continuation driver now that we're idle.
                    _start_goal_driver(session_id)
                except Exception as e:
                    # Any lower-layer failure on the invoke path (command /
                    # mention resolution, stream setup, graph run leaking past
                    # its own handler) surfaces as an in-chat error card instead
                    # of a silently dropped socket.
                    _log.exception("invoke_error session=%s", session_id)
                    try:
                        await ws.send_text(
                            _ev("error", {"message": f"{type(e).__name__}: {e}"})
                        )
                    except Exception:
                        return  # socket died while reporting; nothing to do
            elif kind == "permission_response":
                # The prompt broadcasts to every socket of the session (tabs),
                # so a second response can arrive after the first already
                # resumed the graph — ignore it instead of double-resuming.
                if session_id not in _PENDING_RESUME:
                    continue
                _PENDING_RESUME.discard(session_id)
                decision = msg.get("decision", "deny")
                # resume under the agent that was active when the interrupt fired
                resume_agent = session.get("agent_id") or _first_agent_id()
                resume_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "agent_id": resume_agent,
                    },
                }
                await _run_resume(ws, session["graph"], resume_config, {"decision": decision})
            elif kind == "turn_state":
                # Post-reconnect probe (frontend ChatStream): is a turn still
                # streaming (or parked at an interrupt) for this session? If
                # not, the client reconciles against /history instead of
                # waiting on a stream that will never resume.
                try:
                    await ws.send_text(
                        _ev(
                            "turn.state",
                            {
                                "running": session_id in _RUNNING_TURNS,
                                "turn_id": _RUNNING_TURNS.get(session_id, ""),
                            },
                        )
                    )
                except Exception:
                    return  # socket died between recv and send
            elif kind == "ping":
                try:
                    await ws.send_text(_ev("pong", {}))
                except Exception:
                    return  # socket died between recv and send
            else:
                try:
                    await ws.send_text(_ev("error", {"message": f"unknown type: {kind}"}))
                except Exception:
                    return
    except WebSocketDisconnect:
        return
    finally:
        _log.info("ws_close session=%s", session_id)
        _watch_stop.set()
        _watcher_task.cancel()
        # Drop this socket from the broadcast registry (a dead entry would
        # otherwise linger until the next send attempt pruned it).
        _SESSION_WS[session_id] = [
            w for w in (_SESSION_WS.get(session_id) or []) if w is not ws
        ]


def _resolve_attached_files(
    files: list | None, slug: str, session_id: str
) -> list[dict]:
    """Turn invoke ``files`` items ({id} or {artifact_id} or {name, path})
    into registry-backed entries carrying a compact schema for table kinds."""
    from ..files import extractors as _ex

    reg = files_mod.get_registry(slug)
    out: list[dict] = []
    for f in files or []:
        if not isinstance(f, dict):
            continue
        entry = None
        if f.get("id"):
            entry = files_mod.get_by_id(f["id"])
        # @artifact mention: resolve the artifact's own file ref. Never call
        # add_artifact here — the artifact already exists, and re-adding under
        # a hardcoded kind would duplicate its right-panel row.
        if entry is None and f.get("artifact_id"):
            art = art_store.get_artifact(slug, f["artifact_id"])
            ref = (art.get("ref") or "").strip() if art else ""
            if art and ref:
                p = Path(ref).expanduser()
                if not p.is_file():
                    # Legacy relative ref (workspace-relative) — heal it
                    # before injection, same as the metadata endpoint does.
                    healed = _heal_workspace_ref(art, slug)
                    if healed:
                        ref, p = healed, Path(healed)
                if p.is_file():
                    entry = reg.find_by_path(str(p)) or reg.register(
                        art.get("name") or p.name,
                        p,
                        session_id=session_id,
                        artifact_id=art.get("id"),
                    )
        if entry is None and f.get("path"):
            p = Path(f["path"]).expanduser()
            if p.is_file():
                art = art_store.add_artifact(
                    slug, "file", f.get("name") or p.name, str(p), session_id
                )
                entry = reg.register(
                    f.get("name") or p.name, p, session_id=session_id, artifact_id=art.get("id")
                )
        if entry is None:
            continue
        item = {
            "id": entry["id"],
            "name": entry["name"],
            "path": entry["path"],
            "kind": entry.get("kind") or _ex.classify(entry["path"]),
        }
        # A user-corrected schema (set via the metadata inspector) wins over
        # the auto-computed one — that's the whole point of allowing edits.
        override = ""
        aid = entry.get("artifact_id")
        if aid:
            art = art_store.get_artifact(slug, aid)
            override = ((art.get("schema") or "") if art else "").strip()
        if override:
            item["schema"] = override
        elif item["kind"] in ("spreadsheet", "table"):
            item["schema"] = _compact_schema(entry["path"])
        out.append(item)
    return out


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

    seen: set[str] = set()
    for p in touched:
        if p in seen:
            continue
        seen.add(p)
        for e in files_mod.touch(p, reason=f"tool:{name}"):
            await safe_send(
                emit("preview.invalidate", {"file_id": e["id"], "reason": f"tool:{name}"})
            )


def _maybe_refresh_session_graph(session: dict) -> None:
    """Rebuild the session graph when the live MCP toolset drifts from the
    one frozen into the graph at session-create time.

    The compiled graph binds its ToolNode + model toolset at construction;
    MCP servers that connect LATER (late startup connect, a DNS window that
    healed, a mid-session /api/mcp/reload) never reached existing sessions.
    2026-08-10 incident: the registry held 97 DingTalk tools while a
    research session still offered the single MCP tool it had frozen with,
    and the world diff kept announcing counts the agent could not call.

    Fast path is cheap: ``list_wrapped_tools()`` reads graph-facing tool
    names without constructing langchain wrappers; the heavy rebuild only
    runs on change. Agent tools_allow needs no rebuild — agent_node
    re-resolves the agent and re-filters per step.
    """
    reg = shared._mcp
    if not reg:
        return
    live = sorted(reg.list_wrapped_tools())
    if live == sorted(session.get("mcp_tool_names") or []):
        return
    mcp_tools = reg.all_langchain_tools()
    slug = session.get("project_slug") or "default"
    workspace = str(session.get("workspace") or "")
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=slug,
        session_id=session.get("session_id", ""),
        context_dirs=session.get("context_dirs") or [],
        primary_path=session.get("primary_path") or "",
    )
    session["graph"] = build_graph(
        model=session["model"],
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
        all_tools=all_tools,
    )
    session["all_tool_names"] = [t.name for t in all_tools]
    session["mcp_tool_names"] = [t.name for t in mcp_tools]
    _log.info(
        "graph_refreshed session=%s mcp_tools=%d all_tools=%d",
        session.get("session_id", ""),
        len(mcp_tools),
        len(all_tools),
    )


async def _run_stream(
    ws: WebSocket,
    graph,
    config: dict,
    user_text: str,
    session: dict,
    agent_id: str | None = None,
    images: list | None = None,
    files: list | None = None,
    mention_context: list | None = None,
    skill_name: str | None = None,
) -> None:
    """Append a HumanMessage and stream the agent loop until end or interrupt.

    ``images`` carries ``{"data": <base64>, "media_type": "image/png"}`` items
    from the composer; when present the HumanMessage becomes a multimodal
    content list (OpenAI-style image_url data URLs, which both ChatOpenAI and
    ChatAnthropic accept). ``files`` carries uploaded file refs ({id} or
    {name, path}) resolved via the file registry and injected into the system
    prompt through ``state["attached_files"]``. ``mention_context`` carries
    resolved @mention sections (workflow/memory/non-file artifact) injected
    into the system prompt; ``skill_name`` marks an invoked slash-skill turn.
    """
    content: Any = user_text
    imgs = [i for i in (images or []) if isinstance(i, dict) and i.get("data")]
    if imgs:
        parts: list[dict] = []
        if user_text:
            parts.append({"type": "text", "text": user_text})
        for img in imgs:
            media = img.get("media_type") or "image/png"
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{img['data']}"}}
            )
        content = parts
    attached = _resolve_attached_files(
        files, session["project_slug"], session.get("session_id", "")
    )
    if attached and not (user_text or "").strip():
        # Drop with no text: synthesize a default intent so the turn still runs.
        content = "请概览我附加的文件：结构、数据质量与关键指标，并给出简短结论。"

    session_id = session.get("session_id", "")
    slug = session["project_slug"]
    turn_id = ((config or {}).get("configurable") or {}).get("turn_id")
    effective_agent = agent_id or session.get("agent_id") or ""

    # Lazy MCP healing + graph refresh: a server that connects AFTER session
    # creation (late startup connect, recovered DNS, mid-session reload) must
    # still reach THIS turn's tool bindings. Retry is fire-and-forget with a
    # cooldown inside; a recovered server triggers the graph rebuild on the
    # NEXT turn (its tools only become live once connect_all finishes).
    try:
        if shared._mcp and shared._mcp.has_pending_failures():
            spawn_bg(shared._mcp.retry_failed())
        _maybe_refresh_session_graph(session)
    except Exception:
        _log.exception("graph_refresh_failed session=%s", session_id)
    graph = session["graph"]

    _live_names: dict[str, list[str]] = {}

    def _world_ctx() -> SessionCtx:
        # Live tool/MCP name lists. The session dict freezes these at graph
        # build time — when MCP may still be connecting — which made the world
        # diff oscillate (24→64 / 0→40) and announce phantom changes on every
        # turn. Recompute from the live registries instead (cheap: object
        # construction only, memoized per invoke).
        if not _live_names:
            mcp_tools = shared._mcp.all_langchain_tools() if shared._mcp else []
            _live_names["mcp"] = [t.name for t in mcp_tools]
            _live_names["all"] = [
                t.name
                for t in build_all_tools(
                    mcp_tools,
                    workspace=str(session.get("workspace") or ""),
                    project_slug=slug,
                    session_id=session_id,
                )
            ]
        return SessionCtx(
            session_id=session_id,
            project_slug=slug,
            agent_id=effective_agent or None,
            mcp_tool_names=list(_live_names["mcp"]),
            all_tool_names=list(_live_names["all"]),
            workspace=str(session.get("workspace") or ""),
            context_dirs=list(session.get("context_dirs") or []),
            primary_path=str(session.get("primary_path") or ""),
        )

    # Microcompact — clear stale tool outputs (rung below E3) BEFORE E3
    # measures tokens: if clearing frees enough, the full summary never fires.
    # Pure state rewrite, no LLM call. Same never-a-blocker contract.
    microcompact_stats = None
    try:
        from ..microcompact import maybe_microcompact_history

        microcompact_stats = await maybe_microcompact_history(session, config)
    except Exception:
        _log.exception("microcompact_failed session=%s", session_id)
    if microcompact_stats:
        await _push_session_event(
            session_id,
            "context.microcompacted",
            {
                "cleared_tool_outputs": microcompact_stats["cleared_tool_outputs"],
                "chars_freed": microcompact_stats["chars_freed"],
            },
            turn_id,
        )

    # E3 — history compaction, checked BEFORE this turn's messages land.
    # Never fires while an interrupt is pending (guarded inside). Failures are
    # logged and swallowed: compaction is an optimization, never a blocker.
    compaction_stats = None
    try:
        from ..compaction import maybe_compact_history

        compaction_stats = await maybe_compact_history(session, config, ctx_factory=_world_ctx)
    except Exception:
        _log.exception("compaction_failed session=%s", session_id)
    if compaction_stats:
        await _push_session_event(
            session_id,
            "context.compacted",
            {
                "compacted_messages": compaction_stats["compacted_messages"],
                "kept_messages": compaction_stats["kept_messages"],
            },
            turn_id,
        )

    # C1/C2 — WorldState diff against the session baseline. First sync only
    # records the baseline (the initial system prompt already carries the
    # world); later syncs may yield ONE merged update message + chip event.
    update_text = None
    if context_settings().get("world_state", True):
        try:
            update_text, chip_changes = sync_world_state(_world_ctx())
        except Exception:
            _log.exception("world_state_sync_failed session=%s", session_id)
            update_text, chip_changes = None, []
        if chip_changes:
            await _push_session_event(
                session_id, "context.updated", {"changes": chip_changes}, turn_id
            )

    # B1 — per-turn volatile context (wiki retrieval / attached files /
    # @mentions) rides a tail message instead of the stable system prompt.
    # Citation framework (citations-design.md): begin this turn's source list
    # so wiki injection (and later web tools) can register what the model
    # actually saw — the trailing citation block validates against it.
    # begin() resets the list, which is exactly the retry-with-same-turn-id
    # semantics; an interrupt-parked turn keeps its list until it completes.
    from ..knowledge import citations as _citations_mod

    _turn_sources = _citations_mod.begin_turn_sources(session_id)
    _src_token = _citations_mod.CURRENT_TURN_SOURCES.set(_turn_sources)
    try:
        turn_ctx_text = build_turn_context(
            query=user_text or "",
            attached_files=attached,
            mention_context=mention_context,
        )
    finally:
        _citations_mod.CURRENT_TURN_SOURCES.reset(_src_token)

    messages: list = []
    if compaction_stats and compaction_stats.get("reinject"):
        messages.append(HumanMessage(content=compaction_stats["reinject"]))  # E4
    if update_text:
        messages.append(HumanMessage(content=update_text))
    if turn_ctx_text:
        messages.append(HumanMessage(content=f"{TURN_CONTEXT_PREFIX}\n{turn_ctx_text}"))
    # The actual user message carries the turn id (origin/main retry chain
    # keys on it); the scaffolding messages above stay id-less.
    messages.append(HumanMessage(content=content, **({"id": turn_id} if turn_id else {})))

    input_state = {
        "messages": messages,
        "workspace": session["workspace"],
        "project_slug": session["project_slug"],
        "agent_id": effective_agent,
        "active_skills": [skill_name] if skill_name else [],
        "pending_tool_calls": [],
        "attached_files": attached,
        # Always present (even []) so the channel resets per turn — mentions
        # must not leak into the next turn (same last-value-wins semantics as
        # attached_files; there is no reducer on this key).
        "mention_context": mention_context or [],
        # WorldState mcp section input (A7); persists across steps like the
        # other channels, refreshed on every invoke. Live value (not the
        # session-dict freeze) so the snapshot converges with reality.
        "mcp_tool_names": list(_world_ctx().mcp_tool_names),
        # Mounted context folders (context-folders-design.md): stable within a
        # mount set; a change goes through PUT /sessions/{id}/context (or
        # /mount), which rebuilds the graph and updates the session dict.
        "context_dirs": list(session.get("context_dirs") or []),
        "primary_path": str(session.get("primary_path") or ""),
    }
    await _stream_graph(ws, graph, config, input_state=input_state)


async def _run_resume(ws: WebSocket, graph, config: dict, resume_value: dict) -> None:
    """Resume the graph from a pending interrupt (e.g. permission ask)."""
    await _stream_graph(ws, graph, config, command=Command(resume=resume_value))


async def _process_turn_citations(session_id: str, turn_id: str, text: str) -> None:
    """Parse the trailing ``<ginno_citations>`` block, validate it against the
    turn's registered sources, and record the wiki usage ledger
    (docs/citations-design.md §2-3). Telemetry-only: never raises outward.
    """
    from ..knowledge import citations as cit
    from ..knowledge import usage as kb_usage
    from ..knowledge import web_usage
    from ..knowledge.config import load_knowledge_config

    sources = cit.end_turn_sources(session_id)  # always pop — turn is over
    if not text:
        return
    cfg = load_knowledge_config()
    if not getattr(cfg, "citations", True):
        return
    entries = cit.parse_citation_block(text)
    if not entries:
        return

    resolve_wiki = None
    if cfg.usable:
        def resolve_wiki(ref: str):  # noqa: E306 — index lookup for index_only triage
            try:
                from ..knowledge.indexer import get_indexer

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
    _log.info(
        "turn_citations session=%s turn=%s entries=%d verified=%d invalid=%d",
        session_id,
        turn_id,
        len(validated),
        sum(1 for i in validated if i.get("status") == "verified"),
        len(invalid),
    )


# Max seconds between stream chunks before the stall watchdog aborts the turn
# (see chunked_stream in _stream_graph). Module-level so tests can shrink it.
CHUNK_TIMEOUT_S = 180.0


async def _stream_graph(
    ws: WebSocket,
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Drive the graph and emit token / tool / permission events."""
    # Pre-initialized so the finally-block bookkeeping below can never raise a
    # NameError that masks the original failure.
    saw_interrupt = False
    ws_closed = False
    session_id = (config.get("configurable") or {}).get("thread_id", "")
    try:
        # Per-turn trace id (from invoke, or fresh on a bare resume). `emit`
        # wraps _ev so EVERY event of this turn carries it — the frontend shows
        # it on the bubble and we log it, so a user-supplied UUID greps the logs.
        _cfg_conf = config.get("configurable") or {}
        turn_id = _cfg_conf.get("turn_id") or str(uuid.uuid4())
        _cfg_conf["turn_id"] = turn_id
        config["configurable"] = _cfg_conf

        def emit(event: str, data: dict) -> str:
            return _ev(event, data, turn_id)

        _ensure_turn_log()  # (re)point the trace file handler at the active home

        slug = (config.get("configurable") or {}).get("project_slug", "default")
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        agent_id = (config.get("configurable") or {}).get("agent_id", "")
        # Usage telemetry: continuation turns are tagged by the goal driver;
        # everything else is user-driven "chat" (usage-stats-design.md §3.6).
        usage_source = (config.get("configurable") or {}).get("usage_source") or "chat"
        _RUNNING_TURNS[session_id] = turn_id

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

        if command is not None:
            stream = graph.astream(command, config=config, stream_mode=["messages", "updates"])
        else:
            stream = graph.astream(input_state, config=config, stream_mode=["messages", "updates"])
        saw_interrupt = False
        special_ids: dict[str, str] = {}  # tool_call id -> special tool name (no bubble)
        tool_args_by_id: dict[str, tuple[str, dict]] = {}  # id -> (name, args)
        # tool_call ids that already emitted a tool.start bubble. Parallel tool
        # calls each carry their own id (and a distinct streaming ``index``);
        # keying on id — not ``index == 0`` — ensures every one of a parallel
        # batch gets its bubble (the old ``not index`` check only surfaced the
        # first, silently dropping the rest from the live view).
        started_tool_ids: set[str] = set()
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
                emit("turn.start", {"turn_id": turn_id, "agent_id": _aid or "", "name": _ag.name if _ag else "Agent"})
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
        async def chunked_stream():
            it = stream.__aiter__()
            while True:
                nxt = asyncio.ensure_future(it.__anext__())
                nxt.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )  # mark result retrieved, silence "never retrieved" warnings
                done, _ = await asyncio.wait({nxt}, timeout=CHUNK_TIMEOUT_S)
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
                                await safe_send(emit("token.delta", {"content": txt}))
                elif isinstance(content, str) and content:
                    turn_text.append(content)
                    await safe_send(emit("token.delta", {"content": content}))
                rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if rk:
                    await safe_send(emit("thinking.delta", {"content": rk}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        tc_id = tc.get("id")
                        # Fire one tool.start per distinct tool_call id (the
                        # chunk that carries the name). See started_tool_ids —
                        # keying on id lets parallel tool calls each surface.
                        if tc.get("name") and tc_id and tc_id not in started_tool_ids:
                            started_tool_ids.add(tc_id)
                            if (
                                tc["name"] in RENDER_TOOL_NAMES
                                or tc["name"] in WORKFLOW_TOOL_NAMES
                                or tc["name"] in ARTIFACT_TOOL_NAMES
                            ):
                                special_ids[tc_id] = tc["name"]
                                continue  # surfaced as widget/ref/workflow block, not a tool bubble
                            await safe_send(
                                emit("tool.start", {"name": tc["name"], "id": tc_id})
                            )
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "agent":
                        for m in (delta or {}).get("messages", []):
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
                                    preview = _tool_args_preview(nm, args)
                                    if preview:
                                        await safe_send(
                                            emit("tool.args", {"id": tc_id, "preview": preview})
                                        )
                                if nm == "render_widget":
                                    await safe_send(
                                        emit("widget.emit", {
                                            "kind": args.get("kind", "widget"),
                                            "data": args.get("data"),
                                        })
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
                                    t = _WF_RUN_TASKS.get(run["id"])
                                    if (t is None or t.done()) and run.get("status") == "running":
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
                                    tool_args_by_id.get(tc_id), _tool_content_str(raw),
                                )
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
            try:
                await _process_turn_citations(session_id, turn_id, _final_text)
            except Exception:
                _log.exception("citations_failed session=%s turn=%s", session_id, turn_id)
            # Capture sanitized assistant text for memory summarization (P2).
            # Also reused as the desktop notification body below (the web shell
            # shows a turn-done notification when the user looked away).
            from ..knowledge.citations import strip_citation_block

            _clean_text = strip_citation_block(_final_text)
            if turn_text:
                from ..memory import append_to_pool

                append_to_pool(session_id, agent_id, _clean_text)
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
    except Exception as e:
        _log.exception("turn_error session=%s turn=%s", session_id, turn_id)
        err_msg = f"{type(e).__name__}: {e}"
        # Persist the failure on the session meta so the error card (with its
        # retry action) survives webview reloads and route/session switches —
        # the history endpoint re-surfaces it as the last message.
        _session_meta_patch(
            slug,
            session_id,
            {"last_error": {"turn_id": turn_id, "message": err_msg, "at": time.time()}},
        )
        await safe_send(emit("error", {"message": err_msg}))
    finally:
        try:
            _ka.cancel()
        except Exception:
            pass
        # Ended or errored (not paused at an interrupt): unregister so a client
        # `turn_state` query reports "not running". A turn parked at a
        # permission/version-propose interrupt stays registered — its resume
        # hasn't happened yet.
        if not saw_interrupt:
            _RUNNING_TURNS.pop(session_id, None)
            _PENDING_RESUME.discard(session_id)
        if ws_closed:
            _log.info(
                "turn_client_gone session=%s turn=%s (no live client socket at the "
                "last send; turn completed server-side)",
                session_id,
                turn_id,
            )
