"""Turn-failure classification, user-facing error text, and the turn budget.

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
            return max(25, min(10000, int(raw.strip())))
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
                return max(25, min(10000, int(val)))
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


def recursion_warn_threshold(limit: int) -> int:
    """Agent-step count at which the recursion pre-warn steer fires.

    Each agent round costs ~2 supersteps (agent + tools), so the steer goes
    out once ``2 * agent_steps`` comes within the margin. The margin is
    proportional (10%) with a 10-step floor: the floor alone would warn only
    10 steps from a raised 400-step wall, leaving no room to write the
    wrap-up. Returns the minimum ``agent_steps`` that trips the warning.
    """
    if limit <= 0:
        return 0
    margin = max(10, limit // 10)
    return max(1, -(-(limit - margin) // 2))


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
        # 对齐 web catalog 的 stream.turn_failed_note.<suffix>(平铺键
        # stream.turn_failed 已被通用 case 占用,分类提示必须挂 _note 子树)。
        "i18n_key": f"stream.turn_failed_note.{suffix}",
        "params": {"error": text, **params},
    }


# Provider-side OUTPUT classifier rejection. anthropic raises
# APIStatusError(code="InvalidParameter", message="Output data may contain
# inappropriate content.") MID-STREAM, while the model is writing its answer
# — distinct from a refusal (which completes normally with refusal text) and
# from an input-side rejection (which fails before generation). First seen
# 2026-10-08 turn 4b3a219e: summarizing ten scraped news pages aggregated
# enough text to trip the classifier mid-answer.
#
# NOT transient: the same material is rejected again on retry, so this must
# never ride the auto-retry path (400 status keeps it out of that path
# already — _is_transient_model_error only retries 408/429/5xx).
_CONTENT_FILTER_MARKERS = (
    "output data may contain inappropriate content",
    "output_will_be_blocked",
    "content_policy_violation",
)


def _is_content_filter_error(exc: BaseException) -> bool:
    """True when the provider's OUTPUT filter rejected generated content.

    Walks the __cause__/__context__ chain the same way
    _is_transient_model_error does — langgraph surfaces the model error
    directly, but wrappers (RetryError, task groups) can nest the real one.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        text = str(e).lower()
        if any(m in text for m in _CONTENT_FILTER_MARKERS):
            return True
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None:
            stack.append(e.__context__)
    return False


def _langgraph_error_hint(e: BaseException) -> tuple[str, dict, str] | None:
    """Classify a turn failure for the error card.

    Returns ``(i18n key suffix under stream.turn_failed_note, params,
    English hint)`` or ``None`` for unknown / control-flow exceptions.
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
    if _is_content_filter_error(e):
        return (
            "content_filter",
            {},
            "The provider's safety filter blocked the model's OUTPUT while it"
            " was writing the answer — usually because a large amount of"
            " fetched material (many scraped pages, long documents) was"
            " summarized in one go."
            "\nRetrying the same request will be blocked the same way. Ask for"
            " a narrower slice (fewer pages, or summarize them in batches), and"
            " quote sources instead of pasting long passages.",
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
