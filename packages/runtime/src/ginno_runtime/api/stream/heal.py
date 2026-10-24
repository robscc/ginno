"""Stop / interrupted-turn repair (checkpoint healing).

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
