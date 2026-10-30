"""Subagent lifecycle scheduler (docs/subagent-design.md §5.5-§5.8).

A subagent IS a child session: this module owns everything the plain session
machinery does not already give it —

* spawn: depth check → concurrency cap (5, hard) → child session creation
  (delegates to ``api.sessions.create_session`` so the meta/graph/toolset are
  built exactly like any session) → standalone brief → background turn;
* the STRUCTURAL completion gate (§5.6): a subagent is done only when its own
  turn has ended AND no descendant is still running; otherwise it parks in
  ``waiting`` until the last descendant's result wakes it with a new turn;
* result injection into the parent (§5.5): ``steer_enqueue`` when the parent is
  running, a new system-side turn when it is idle — the same two channels user
  steering uses;
* stop cascade and cascade delete (§5.7/§5.8) over the parent_session_id tree.

Import discipline: this module sits under graph/tools in the import order
(``graph.build_all_tools`` pulls in ``tools.subagent``), so api.stream /
api.sessions are imported LAZILY inside the functions that need them.

Split into submodules by topic (state/brief/meta/reconcile/fork/spawn/runner/
gate/stop); this ``__init__`` keeps the original single-module surface — every
former top-level name stays an attribute of ``ginno_runtime.subagent_scheduler``.
Submodules resolve every former module global through this package namespace
(``_NS = sys.modules[__package__]``) AT CALL TIME, so a monkeypatch on any
attribute here keeps reaching the internal call sites, and the mutable state
(locks / pending queues / seen sets) stays a single instance."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths, server_shared
from ..lang import t
from ..server_shared import (
    _MANAGED_TURNS,
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _STEER_STASH,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    spawn_bg,
    steer_enqueue,
)
from ..session_meta import (
    _find_meta,
    _session_meta_descendants,
    _session_meta_patch,
    _session_meta_remove,
    _session_slug,
    subagent_depth_of,
)
from ._state import (
    _INJECTION_FLUSH_TASKS,
    _INJECTION_WAKING,
    _PENDING_INJECTIONS,
    _RECURSION_WRAP_TEXT,
    _RECURSION_WRAP_TEXT_EN,
    _SPAWN_LOCK,
    _WARNED_CONCURRENCY,
    INJECTION_COALESCE_S,
    LIVE_STATUSES,
    SUBAGENT_MAX_CONCURRENT,
    SUBAGENT_MAX_CONCURRENT_LIMIT,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_MIN_CONCURRENT,
    SUBAGENT_TITLE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TERMINAL_STATUSES,
    WARN_RUNNING_RATIO,
)
from .brief import (
    _OUTPUT_DISCIPLINE,
    _OUTPUT_DISCIPLINE_EN,
    _REPORT_FORMAT,
    _REPORT_FORMAT_EN,
    _xml_attr,
    _xml_text,
    build_subagent_brief,
    format_subagent_failure,
    format_subagent_result,
)
from .fork import (
    _fork_copy,
    _fork_id,
    _fork_start_text,
    _heal_fork_tail,
    _seed_fork_history,
)
from .gate import (
    _FINALIZING,
    _SUBAGENT_TITLE_SYSTEM,
    _SUBAGENT_TITLE_SYSTEM_EN,
    _final_report_text,
    _finalize_subagent,
    _finalize_subagent_locked,
    _gen_subagent_title,
    _mark_waiting,
    _prepare_report,
    _write_report_file,
    on_stop_if_waiting,
    on_turn_settled,
    on_turn_stopped,
    revive_failed_for_retry,
)
from .meta import (
    _clamp_concurrent,
    _concurrency_warn_floor,
    _is_subagent,
    _live_descendants,
    _maybe_warn_concurrency,
    _push_both,
    _running_subagent_metas,
    _subagent_status,
    _touch_in_memory,
    _tree_ids,
    _wait_rows,
    _warn_once,
    collect_subagent_rows,
    max_concurrent,
    report_settings,
)
from .reconcile import (
    _is_subagent_injection,
    reconcile_orphan_subagents,
    reconcile_stash_injections,
)
from .runner import (
    _flush_injections,
    _inject_result,
    _injection_flush_timer,
    _queue_injection,
    _reevaluate_waiting_parent,
    _run_managed_turn,
    _run_managed_turn_locked,
    _turn_lock_of,
    _wake_parent_turn,
)
from .spawn import _agent_type_model_override, create_subagent
from .stop import (
    _stop_parked_descendant,
    delete_subagent_tree,
    finalize_for_delete,
    stop_descendants,
    stop_subagent_tree,
    wait_for_subagents,
)
