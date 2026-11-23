"""The streaming engine: _stream_graph, the chunked stream + stall watchdog.

Split out of the former single-module ``api/stream.py`` (pure structural
move — no behavior change). The helper groups it used to carry now live in
sibling modules (``.errors`` turn-error classification/budget/auto-retry,
``.narration`` server-search + steer helpers, ``.effects`` per-turn side
effects, ``.heal`` stop/interrupted-turn repair); they are re-imported below
so ``engine.X`` keeps working. The package ``__init__`` re-exports the whole
historical namespace and mirrors patch writes into the submodules, so
``ginno_runtime.api.stream.X`` keeps working as the import/patch surface.
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
from ...models import responses_reasoning_summary
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _RUNNING_TURNS_AT,
    _SESSION_WS,
    _SESSIONS,
    _TURN_STOP,
    _USAGE_BY_SESSION,
    _WF_RUN_TASKS,
    _ensure_turn_log,
    _journal_frame,
    _ev,
    _log,
    _push_global_event,
    _push_session_event,
    _try_send,
    _turn_lock,
    spawn_bg,
)
from ...session_meta import _find_meta, _session_meta_patch, resolve_session_model
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
from ...usage import add_usage, cache_hit_ratio, context_state, empty_usage, extract_usage
from ...workflows import store as wf_storemod
from .. import server_tools
from ..files import _normalize_file_ref, _register_artifact_file, _session_workspace
from ..messages_ui import _tool_args_preview, _tool_content_str, _truncate_for_ws
from .effects import (
    _AUTO_DISTILL_FAIL_AT,
    _AUTO_DISTILL_THROTTLE_S,
    _emit_code_changes,
    _maybe_auto_distill,
    _process_turn_citations,
    _tool_file_effects,
)
from .errors import (
    _CONTENT_FILTER_MARKERS,
    _TRANSIENT_EXC_NAMES,
    AUTO_RETRY_BACKOFF_S,
    AUTO_RETRY_MAX,
    TURN_RECURSION_LIMIT_DEFAULT,
    TurnStopped,
    clean_model_error_text,
    _is_content_filter_error,
    _is_transient_model_error,
    _langgraph_error_hint,
    format_turn_error,
    model_error_reset_at,
    recursion_warn_threshold,
    turn_error_fields,
    turn_recursion_limit,
)
from .event_sink import get_sink
from .heal import _heal_interrupted_turn, _stop_parked_turn
from .narration import (
    _SERVER_SEARCH_BLOCK_TYPES,
    STEER_CONT_MAX,
    STEER_ID_SYSTEM_PREFIX,
    _emit_glm_server_search,
    _is_user_steer,
    _server_search_hits,
)

# Max seconds between stream chunks before the stall watchdog aborts the turn
# (see chunked_stream in _stream_graph). Module-level so tests can shrink it.
# 流断流看门狗:chunk 间隔超过该值即判死(防挂死 turn)。慢网关/限流退避
# (SDK+代理重试可达数分钟无 chunk)会误伤,可用 GINNO_CHUNK_TIMEOUT_S 抬高
# (ginno exec benchmark 模式默认抬高到 1200s,见 cli.py)。
CHUNK_TIMEOUT_S = float(os.environ.get("GINNO_CHUNK_TIMEOUT_S", "180"))


async def _stream_graph(
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Turn 流引擎入口（原批次 5 特征开关分叉点，legacy 路已删除）。

    唯一引擎路径：``api/stream/stages/`` 四阶段管道（permission →
    toolloop → streamio → finalize）。批次 5-2 复制式迁移 + 5-3
    AsyncExitStack 栈化，golden 帧序契约与旧路逐字节一致；legacy
    巨函数（约 1650 行）经 dev-swap 真机观察期后删除。

    保留本薄壳的意义：所有既有调用方/测试 patch 面（engine.
    ``_stream_graph``）不变；延迟 import 防 stages 回引环。
    """
    # 延迟 import：stages 包自身回引 engine 侧名字，避免环。
    from .stages import stream_graph_staged

    await stream_graph_staged(
        graph, config, input_state=input_state, command=command
    )
