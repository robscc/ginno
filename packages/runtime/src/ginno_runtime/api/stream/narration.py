"""Provider-side search blocks and the steering narration helpers.

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

# Content-block types carrying provider-side web-search results. The Anthropic
# protocol returns them as `web_search_tool_result`; the `server_tool_use` /
# `tool_use` pair above it is the request echo, not a result.
_SERVER_SEARCH_BLOCK_TYPES = frozenset(
    {"web_search_tool_result", "web_fetch_tool_result"}
)


def _server_search_hits(block: dict, block_types: frozenset[str]) -> list[dict]:
    """Extract ``{url, title, content, page_age}`` from a server-search block.

    Tolerant by necessity: an Anthropic-compatible gateway that relays these
    blocks without the official envelope makes the SDK parse the items as
    ``WebSearchToolResultError`` — still carrying url/title/content, so the
    fields are read from either a dict or a pydantic model. Items whose
    ``type`` marks them as a search *failure* are dropped; so are the
    non-list payloads a gateway may substitute.
    """
    btype = block.get("type") if isinstance(block, dict) else None
    if btype not in block_types:
        return []
    payload = block.get("content")
    if isinstance(payload, dict):
        payload = payload.get("content")
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for it in payload:
        d = it if isinstance(it, dict) else getattr(it, "model_dump", lambda: {})()
        if not isinstance(d, dict):
            continue
        # `error_code` set, or a type that isn't a result, means the search
        # itself failed — nothing citable in it.
        if d.get("error_code") or (d.get("type") and d.get("type") != "web_search_result"):
            continue
        if d.get("url"):
            out.append(
                {
                    "url": d.get("url"),
                    "title": d.get("title") or "",
                    "content": d.get("content") or "",
                    "page_age": d.get("page_age") or "",
                }
            )
    return out


async def _emit_glm_server_search(
    safe_send, emit, session_id: str, turn_id: str, m: Any, started: set[str]
) -> None:
    """Z.ai/GLM gateway server-side search (``server_tool_use`` + ``tool_result``
    inside the assistant message): register the hits under the same contract as
    the Anthropic-native blocks, then close the tool bubble opened on the
    streamed server_tool_use with the hits rendered in the standard
    ``[sN] Title — host`` contract, so ``web|sN`` citations resolve both live
    and on history replay."""
    for grp in server_tools.glm_server_search_groups(getattr(m, "content", None)):
        registered = cit.register_server_search_hits(session_id, grp["hits"])
        id_by_url = {s["identity"]: s["id"] for s in registered}
        pairs = [(id_by_url.get(h["url"]), h) for h in grp["hits"]]
        content = server_tools.format_hits_block(grp["name"], grp["query"], pairs)
        tid = grp["tool_id"]
        if tid and tid not in started:
            started.add(tid)
            await safe_send(
                emit("tool.start", {"name": server_tools.DISPLAY_TOOL_NAME, "id": tid})
            )
        if tid and grp["query"]:
            preview = _tool_args_preview(
                server_tools.DISPLAY_TOOL_NAME, {"query": grp["query"]}
            )
            if preview:
                await safe_send(emit("tool.args", {"id": tid, "preview": preview}))
        await safe_send(
            emit("tool.end", {"id": tid, "content": _truncate_for_ws(content)})
        )
        _log.info(
            "server_search session=%s turn=%s engine=%s hits=%d urls=%s",
            session_id,
            turn_id,
            grp["name"],
            len(grp["hits"]),
            ",".join(h["url"] for h in grp["hits"])[:400],
        )


# How many times one turn may be resumed with a fresh step budget after a
# USER steer pushed it past the limit. A steered turn is doing more than the
# budget was sized for, so dying at the wall would silently drop the user's
# own request; but an unbounded refresh lets a runaway loop live forever.
# Each resume preserves tool work by continuing from the latest checkpoint.
STEER_CONT_MAX = 2

# steer_id namespace for Ginno-injected steering (as opposed to a human
# steer). Prefixed so _steer_absorbed can tell the two apart.
STEER_ID_SYSTEM_PREFIX = "recursion-warn-"


def _is_user_steer(entry: dict) -> bool:
    """True when a drained steering entry came from the human.

    Ginno-injected steering (the recursion pre-warn) is tagged with
    STEER_ID_SYSTEM_PREFIX. An entry with no steer_id is treated as user —
    conservative, because granting budget is the less harmful miss.
    """
    return not str(entry.get("steer_id") or "").startswith(STEER_ID_SYSTEM_PREFIX)
