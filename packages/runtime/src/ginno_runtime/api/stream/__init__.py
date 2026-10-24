"""Session WebSocket + the per-turn streaming engine.

The WS endpoint accepts invoke / stop / permission_response / turn_state /
ping messages; the streaming engine drives the LangGraph agent loop and
broadcasts token / tool / permission / usage events to every live socket of
the session.

This used to be one module (``api/stream.py``); it is now a package split
along its coupling seams, with **zero behavior change**:

- :mod:`.ws` — the WebSocket endpoint (``session_ws``) + session title touch.
- :mod:`.turn` — turn preparation: steer payload / attached files / MCP graph
  refresh / ``_run_stream`` / ``_run_resume``.
- :mod:`.engine` — the streaming engine: ``_stream_graph``, the chunked
  stream + stall watchdog.
- :mod:`.errors` — turn-failure classification, error-card payload, the
  recursion/turn budget and transient-failure auto-retry predicates.
- :mod:`.narration` — provider-side search block normalization and the
  steering narration helpers.
- :mod:`.effects` — per-turn side effects: file previews / ``code.changed``,
  citations, auto-distill.
- :mod:`.heal` — stop / interrupted-turn checkpoint repair.

Two structural contracts keep the outside world unchanged:

1. **Import surface** — every name the old single module carried is
   re-exported here, so ``from ginno_runtime.api.stream import X`` and
   ``ginno_runtime.api.stream.X`` (tests, ``server.py``, the subagent
   scheduler's lazy imports) keep working verbatim.
2. **Patch surface** — tests monkeypatch attributes on this package
   (``CHUNK_TIMEOUT_S``, ``AUTO_RETRY_*``, ``_run_stream``, ``_run_resume``,
   ``_stop_parked_turn``, ``_touch_session_title``, ``build_graph``,
   ``_push_session_event``, ``_session_meta_patch``, ...). Before the split a
   patch changed the one global the runtime code reads; after the split the
   readers live in the submodules, so :class:`_PatchMirrorModule` mirrors
   every attribute write/delete on this package into each submodule whose
   namespace carries the name (and monkeypatch's restore mirrors back the
   same way). In-package code keeps reading plain module globals, so patches
   behave exactly as they did against the monolith.
"""

import sys
from types import ModuleType

from .effects import (  # noqa: F401  (re-export of the historical namespace)
    _AUTO_DISTILL_FAIL_AT,
    _AUTO_DISTILL_THROTTLE_S,
    _emit_code_changes,
    _maybe_auto_distill,
    _process_turn_citations,
    _tool_file_effects,
)

# --- Names the old module had in its namespace via imports -------------------
# Re-exported so that monkeypatch targets which existed on the monolith (tests
# patch ``build_graph``, ``_push_session_event``, ``_session_meta_patch`` ...)
# still exist on the package, and any external reader of these attributes
# keeps seeing them.
from .engine import (  # noqa: F401  (re-export of the historical namespace)  # noqa: F401  (re-export of the historical namespace)
    CHUNK_TIMEOUT_S,
    _find_meta,
    _session_meta_patch,
    _stream_graph,
    add_usage,
    agents_reg,
    art_store,
    begin_ask_budget,
    cache_hit_ratio,
    empty_usage,
    extract_usage,
    paths,
    todo_store,
    usage_store,
    wf_store,
    wf_storemod,
    widget_event,
)
from .errors import (  # noqa: F401  (re-export of the historical namespace)
    _CONTENT_FILTER_MARKERS,
    _TRANSIENT_EXC_NAMES,
    AUTO_RETRY_BACKOFF_S,
    AUTO_RETRY_MAX,
    TURN_RECURSION_LIMIT_DEFAULT,
    TurnStopped,
    _is_content_filter_error,
    _is_transient_model_error,
    _langgraph_error_hint,
    format_turn_error,
    recursion_warn_threshold,
    turn_error_fields,
    turn_recursion_limit,
)
from .heal import (  # noqa: F401  (re-export of the historical namespace)
    _heal_interrupted_turn,
    _stop_parked_turn,
)
from .narration import (  # noqa: F401  (re-export of the historical namespace)
    _SERVER_SEARCH_BLOCK_TYPES,
    STEER_CONT_MAX,
    STEER_ID_SYSTEM_PREFIX,
    _emit_glm_server_search,
    _is_user_steer,
    _server_search_hits,
)
from .turn import (  # noqa: F401  (re-export of the historical namespace)  # noqa: F401  (re-export of the historical namespace)
    _attach_only_text,
    _bound_workflow_view,
    _maybe_refresh_session_graph,
    _prepare_steer_payload,
    _resolve_attached_files,
    _run_resume,
    _run_stream,
    build_all_tools,
    build_graph,
    build_turn_context,
    files_mod,
    projects_mod,
    shared,
)
from .ws import (  # noqa: F401  (re-export of the historical namespace)  # noqa: F401  (re-export of the historical namespace)
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _TURN_STOP,
    _TURN_TASKS,
    ABANDONED_TURNS,
    _commands,
    _emit_goal_event,
    _ensure_session,
    _ev,
    _first_agent_id,
    _log,
    _push_session_event,
    _start_goal_driver,
    _touch_session_title,
    _turn_lock,
    files_mod,
    goal_store,
    router,
    session_ws,
    spawn_bg,
)

_MIRROR_TARGETS = (
    sys.modules[__name__ + ".engine"],
    sys.modules[__name__ + ".turn"],
    sys.modules[__name__ + ".ws"],
    sys.modules[__name__ + ".errors"],
    sys.modules[__name__ + ".narration"],
    sys.modules[__name__ + ".effects"],
    sys.modules[__name__ + ".heal"],
)


class _PatchMirrorModule(ModuleType):
    """Mirror attribute writes on the package into the sibling submodules.

    See the module docstring (patch-surface contract). ``monkeypatch.setattr``
    and ``monkeypatch.delattr`` on ``ginno_runtime.api.stream.<name>`` (and
    restores) are mirrored into every submodule whose namespace carries
    ``<name>``, so the streaming code — which reads plain module globals —
    observes patches exactly as it did when this was one module.
    """

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for _sub in _MIRROR_TARGETS:
            if name in vars(_sub):
                setattr(_sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for _sub in _MIRROR_TARGETS:
            if name in vars(_sub):
                delattr(_sub, name)


sys.modules[__name__].__class__ = _PatchMirrorModule
