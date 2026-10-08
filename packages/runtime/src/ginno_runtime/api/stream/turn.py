"""Turn preparation: steer payloads, attached files, graph refresh, _run_stream.

Split out of the former single-module ``api/stream.py`` (pure structural
move — no behavior change). The package ``__init__`` re-exports the whole
historical namespace and mirrors patch writes into the submodules, so
``ginno_runtime.api.stream.X`` keeps working as the import/patch surface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import WebSocket
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ... import artifacts as art_store
from ... import files as files_mod
from ... import projects as projects_mod
from ... import server_shared as shared
from ... import workflows as wf_store
from ...graph import build_all_tools, build_graph, build_turn_context
from ...lang import t
from ...server_shared import _log, _push_session_event, spawn_bg
from ...world_state import (
    TURN_CONTEXT_PREFIX,
    SessionCtx,
    context_settings,
    sync_world_state,
)
from ..files import _compact_schema, _heal_workspace_ref
from .engine import _stream_graph

# Default intent for an attachment-only send (no text typed): the picture or
# document IS the request. Same wording as the turn-start fallback in
# _run_stream — a mid-turn steer must not behave differently just because it
# happened while the agent was working. Bilingual via t() (lang.py): the text
# is BOTH the user-visible bubble copy and the model's instruction, so it
# follows the request locale (i18n 分流规则 — prompt/model-context text uses
# inline t(), never a catalog key).
_ATTACH_ONLY_TEXT_EN = (
    "Summarize the files I attached: structure, data quality, and key metrics, "
    "then give a short conclusion."
)
_ATTACH_ONLY_TEXT_ZH = "请概览我附加的文件：结构、数据质量与关键指标，并给出简短结论。"


def _attach_only_text() -> str:
    return t(_ATTACH_ONLY_TEXT_EN, _ATTACH_ONLY_TEXT_ZH)


async def _prepare_steer_payload(
    text: str,
    images: list | None,
    files: list | None,
    slug: str,
    session_id: str,
) -> dict:
    """Turn a steered message's attachments into MODEL-facing payloads.

    Why here, and not in ``graph.agent_node``: the model only ever sees
    attachments through shapes built at the START of a turn — images are merged
    into that turn's entry HumanMessage multimodal content, documents become a
    ``[turn context]`` message via :func:`build_turn_context` — and the node
    that absorbs a mid-turn message cannot rebuild either for it. The node also
    cannot import this module (stream imports graph → cycle), so the conversion
    happens here, at enqueue time, and rides the stash entry as plain data (the
    brief's "path B").

    This is SYNC FILE IO (registry lookups, schema extraction, artifact
    writes), so it MUST be awaited OFF the WebSocket receive loop — see the
    ``asyncio.to_thread`` below: run inline it would stall keepalive and the
    ``stop`` handler for every socket of the session.

    Returns the stash-entry fields: ``text`` (the user's words, or the default
    intent when attachments came alone), ``content_blocks`` (multimodal content
    list — text first, then OpenAI-style ``image_url`` blocks — or None for a
    plain-text steer), ``context_text`` (the ``[turn context]`` body for the
    attached documents, without the prefix, or None) and the transcript
    summaries ``files`` / ``images``.

    The summaries carry METADATA ONLY — deliberately no image payload. The bytes
    are already in the injected HumanMessage's ``image_url`` block, and the
    checkpointer rewrites the whole session file per step, so a second copy in
    ``additional_kwargs`` would double every steered message for good (the same
    reason the frontend downsizes images before sending them). A replay that
    wants a thumbnail takes it from the content block.
    """
    imgs = [i for i in (images or []) if isinstance(i, dict) and i.get("data")]
    wanted = [f for f in (files or []) if isinstance(f, dict)]
    text = (text or "").strip()
    if not text and (imgs or wanted):
        text = _attach_only_text()

    content_blocks: list[dict] | None = None
    if imgs:
        parts: list[dict] = [{"type": "text", "text": text}] if text else []
        for img in imgs:
            media = img.get("media_type") or "image/png"
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{img['data']}"},
                }
            )
        content_blocks = parts

    attached: list[dict] = []
    if wanted:
        # Off the receive loop: this touches the file registry, the artifact
        # store and (for spreadsheet/table kinds) the file itself for the schema
        # summary — enough to freeze a turn's worth of ping/stop handling.
        attached = await asyncio.to_thread(
            _resolve_attached_files, wanted, slug, session_id
        )
    context_text = build_turn_context(attached_files=attached) if attached else None
    return {
        "text": text,
        "content_blocks": content_blocks,
        "context_text": context_text,
        # Replay summaries, shaped to match the LIVE band the frontend builds
        # from its own queue item ({files: [{id,name,path,kind}], images:
        # [{name, url}]}) so one renderer serves both. ``kind`` comes from the
        # registry / kind classifier inside _resolve_attached_files, so it
        # agrees with the file chips the turn-start bubble renders.
        "files": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "path": a.get("path"),
                "kind": a.get("kind"),
            }
            for a in attached
        ],
        "images": [
            {"name": i.get("name") or "", "media_type": i.get("media_type") or "image/png"}
            for i in imgs
        ],
    }


def _resolve_attached_files(
    files: list | None, slug: str, session_id: str
) -> list[dict]:
    """Turn invoke ``files`` items ({id} or {artifact_id} or {name, path})
    into registry-backed entries carrying a compact schema for table kinds."""
    from ...files import extractors as _ex

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




def _maybe_refresh_session_graph(session: dict) -> None:
    """Rebuild the session graph when the live MCP toolset drifts from the
    one frozen into the graph at session-create time.

    The compiled graph binds its ToolNode + model toolset at construction;
    MCP servers that connect LATER (late startup connect, a DNS window that
    healed, a mid-session /api/mcp/reload) never reached existing sessions.
    2026-08-10 incident: the registry held 97 DingTalk tools while a
    research session still offered the single MCP tool it had frozen with,
    and the world diff kept announcing counts the agent could not call.

    Drift rule (2026-10-07): rebuild only on GAINS (new tools appeared) or on
    servers removed from the config. A pure LOSS (server configured but down —
    DNS blip, restart window) must NOT strip tools from the session: the model
    keeps seeing them, calls fail with a recoverable "not connected" message,
    and the wrappers re-route to the fresh connection on heal (call-time
    resolution in registry._wrap_tool) — no rebuild, no mid-conversation
    toolset churn that breaks the provider cache prefix and the model's
    narrative continuity.

    Fast path is cheap: ``list_wrapped_tools()`` reads graph-facing tool
    names without constructing langchain wrappers; the heavy rebuild only
    runs on real drift. Agent tools_allow needs no rebuild — agent_node
    re-resolves the agent and re-filters per step.
    """
    reg = shared._mcp
    if not reg:
        return
    live = set(reg.list_wrapped_tools())
    frozen = set(session.get("mcp_tool_names") or [])
    gained = live - frozen
    orphaned = reg.unconfigured_wrapped_names(frozen)
    if not gained and not orphaned:
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
        # Keep the structural spawn cap across rebuilds (subagent-design.md §4.2).
        subagent_depth=session.get("depth") if session.get("type") == "subagent" else None,
        # And the type-registry tightening (P3 contract 1) — an MCP-drift
        # rebuild must not hand a restricted child its tools back.
        restrict_tools=session.get("restrict_tools") or [],
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


def _bound_workflow_view(session: dict) -> dict | None:
    """Load the session-bound workflow definition for turn-context injection.

    Missing / unknown ids return None so an unbound session stays quiet.
    """
    wid = session.get("workflow_id")
    if not wid:
        return None
    try:
        wf = wf_store.get_def(wid)
    except Exception:
        _log.exception("bound_workflow_load_failed session=%s workflow=%s", session.get("session_id"), wid)
        return None
    return wf


async def _run_stream(
    ws: WebSocket | None,
    graph,
    config: dict,
    user_text: str,
    session: dict,
    agent_id: str | None = None,
    images: list | None = None,
    files: list | None = None,
    mention_context: list | None = None,
    skill_name: str | None = None,
    user_extra_kwargs: dict | None = None,
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

    ``user_extra_kwargs`` merges machine metadata into the entry HumanMessage's
    ``additional_kwargs`` — the subagent scheduler's result-injection wake turn
    tags its message with ``ginno_subagent_result`` (contract 3); user turns
    never set it.
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
        # Shared with the mid-turn steer path (_prepare_steer_payload) so an
        # attachment-only send reads the same either way.
        content = _attach_only_text()

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
                    subagent_depth=(
                        session.get("depth") if session.get("type") == "subagent" else None
                    ),
                    # Keep the roster consistent with the BOUND toolset: a
                    # type-restricted subagent session (P3 contract 1) must
                    # not advertise tools its graph cannot call.
                    restrict_tools=session.get("restrict_tools") or [],
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
        from ...microcompact import maybe_microcompact_history

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
        from ...compaction import maybe_compact_history

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
    from ...knowledge import citations as _citations_mod

    _turn_sources = _citations_mod.begin_turn_sources(session_id)
    _src_token = _citations_mod.CURRENT_TURN_SOURCES.set(_turn_sources)
    try:
        turn_ctx_text = build_turn_context(
            query=user_text or "",
            attached_files=attached,
            mention_context=mention_context,
            bound_workflow=_bound_workflow_view(session),
            # Repos the file/shell tools reached by absolute path this session
            # (projects.py). Empty for a fresh session → zero-cost.
            projects=projects_mod.known(slug, session_id),
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
    user_kwargs: dict = {}
    if user_extra_kwargs:
        user_kwargs["additional_kwargs"] = dict(user_extra_kwargs)
    messages.append(
        HumanMessage(content=content, **({"id": turn_id} if turn_id else {}), **user_kwargs)
    )

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
    await _stream_graph(graph, config, input_state=input_state)


async def _run_resume(ws: WebSocket | None, graph, config: dict, resume_value: dict) -> None:
    """Resume the graph from a pending interrupt (e.g. permission ask)."""
    await _stream_graph(graph, config, command=Command(resume=resume_value))
