"""Session endpoints —— 资源 Router 聚合包（重构批次 2-1）。

原 api/sessions.py（2133 行 / 28 端点）按绞杀者模式拆为共享基座 + 五个
资源模块；本文件保持 ``ginno_runtime.api.sessions`` 的全部既有名字与
import 路径，下游 import 点零改动：

- ``_base``：全包唯一 router + 跨模块基建（_ensure_session 等）；
- ``goal``：goal 端点 + continuation driver（原文件 228-703 行）；
- ``crud``：建会话 / 列表 / 分页 / 搜索 / 单获取（705-1075）；
- ``meta``：tags / 已读未读 / PATCH / 运行状态 / 工具策略 / 账本 / 建议 /
  历史（1077-1167、1196-1333、1631-1718、1980-2013）；
- ``relations``：fork / linked / context folders（1169-1194、1335-1373、
  1833-1976）；
- ``delegations``：external-agents / 委托停止 / 删除 / 批量 / 停止
  （1375-1627、1719-1828）。

端点注册顺序 = 资源模块 import 顺序（goal → crud → meta → relations →
delegations），与拆分前的注册序同构；唯一的路径遮蔽约束
（/api/sessions/page、/search 先于 /{session_id}）落在 crud 模块内部，
段内逐字节保序。OpenAPI 契约快照零漂移是本拆分的硬验收门。

monkeypatch 兼容（关键设计）：40+ 处测试以 ``ginno_runtime.api.sessions.
<name>`` 为 patch 面（build_model ×20+、_ensure_session ×20+、
GOAL_GRACE_S、_turn_last_error、create_session、stop_session……）。拆分后
真实调用点在资源模块自己的命名空间，写包属性不会自动穿透——故装配尾部
把本包 ``__class__`` 换成 ``_PatchFanoutModule``：对 fanout 名单内的名字，
写包属性同步广播到绑定了该名字的资源模块，monkeypatch / teardown 还原 /
直接赋值三种形态全部保持拆分前语义（conftest._ORIG_BUILD_MODEL 的跨测试
还原同样依赖这条通路）。
"""

from __future__ import annotations

import sys as _sys
import types as _types

from ._base import router  # noqa: F401 — 聚合入口；router 先于资源模块就位

# 注册序 = import 序（goal 端点在原文件中先于 CRUD 端点注册；顺序不可按字母排）。
from . import goal, crud, meta, relations, delegations  # noqa: E402,F401 isort: skip
from . import _base  # noqa: F401 — fanout 目标（模块对象）

# ---- 定义名 re-export（保持 ginno_runtime.api.sessions.<name> 全量可用）------
from ._base import (  # noqa: F401,E402
    _agent_icon,
    _build_model_with_fallback,
    _default_title,
    _ensure_session,
    _first_agent_id,
    _stored_effort,
    is_channel_session,
)
from .crud import (  # noqa: F401,E402
    CreateSessionRequest,
    _remote_peer_name,
    _resolve_provider_model,
    _resolve_thinking_effort,
    _validate_skill_paths,
    _with_remote_peer_name,
    create_session,
    get_session,
    list_sessions,
    page_sessions,
    search_sessions,
)
from .goal import (  # noqa: F401,E402
    GOAL_GRACE_S,
    _USAGE_LIMITED_MARKERS,
    _archive_complete_goal,
    _emit_goal_event,
    _goal_agent,
    _goal_driver_loop,
    _goal_error_status,
    _goal_interrupted,
    _goal_listener,
    _goal_session_remote,
    _goal_slug,
    _run_goal_turn,
    _start_goal_driver,
    _stop_goal_driver,
    _turn_last_error,
    clear_session_goal,
    get_session_goal,
    set_session_goal,
)
from .meta import (  # noqa: F401,E402
    PatchSessionRequest,
    SESSION_TAG_ICONS,
    ToolPolicyBody,
    get_context_breakdown,
    get_session_history,
    get_session_state,
    mark_session_seen_endpoint,
    mark_session_unread_endpoint,
    patch_session,
    session_suggestions,
    set_session_tags,
    set_tool_policy,
)
from .relations import (  # noqa: F401,E402
    MountLinkedRequest,
    PutSessionContextRequest,
    _apply_context_to_live_session,
    apply_session_context,
    detach_linked_session,
    fork_session_endpoint,
    get_linked_sessions,
    get_session_context,
    mount_linked_session,
    put_session_context,
)
from .delegations import (  # noqa: F401,E402
    BulkSessionsRequest,
    _heal_run_history_refs,
    bulk_sessions,
    delete_session,
    list_external_agents,
    stop_delegation,
    stop_session,
)

# ---- 透传名 re-export（原模块顶层 import 的名字，外部曾以 sessions.<name> 访问）--
from ... import agents as agents_reg  # noqa: F401,E402
from ... import context_folders as cf  # noqa: F401,E402
from ... import paths  # noqa: F401,E402
from ... import providers as prov_mod  # noqa: F401,E402
from ... import server_shared as shared  # noqa: F401,E402
from ...agents.memory import ensure_agent_memory  # noqa: F401,E402
from ...checkpointer import FileCheckpointer  # noqa: F401,E402
from ...classifier.middlewares import build_middlewares  # noqa: F401,E402
from ...goals import events as goal_events  # noqa: F401,E402
from ...goals import store as goal_store  # noqa: F401,E402
from ...goals import templates as goal_templates  # noqa: F401,E402
from ...graph import build_all_tools  # noqa: F401,E402
from ...graph import build_graph  # noqa: F401,E402
from ...lang import t  # noqa: F401,E402
from ..messages_ui import _messages_to_ui  # noqa: F401,E402
from ...models import build_model  # noqa: F401,E402
from ...models import normalize_effort  # noqa: F401,E402
from ...server_shared import (  # noqa: F401,E402
    _GOAL_DRIVERS,
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ...session_meta import (  # noqa: F401,E402
    _find_meta,
    _resolve_session_meta,
    _session_meta_list,
    _session_meta_patch,
    _session_meta_remove,
    _session_meta_set_tags,
    _session_meta_upsert,
    _session_slug,
    _tags_fields,
    subagent_depth_of,
)
from ...session_unread import is_unread as _session_is_unread  # noqa: F401,E402
from ...session_unread import mark_session_seen as _mark_session_seen  # noqa: F401,E402
from ...session_unread import mark_session_unread as _session_unread_mark  # noqa: F401,E402
from ...subagent_scheduler import (  # noqa: F401,E402
    SUBAGENT_MAX_DEPTH,
    _is_subagent,
    _subagent_status,
    stop_subagent_tree,
)
from ...tools.ask_tools import reset_interactive as _reset_interactive_turn  # noqa: F401,E402
from ...tools.ask_tools import set_interactive as _set_interactive_turn  # noqa: F401,E402
from ..config import _agent_lookup  # noqa: F401,E402


class _PatchFanoutModule(_types.ModuleType):
    """包属性写入 → 广播到绑定了该名字的资源模块（见模块 docstring）。

    monkeypatch.setattr / 直接赋值 / teardown 还原全部经由 __setattr__，
    fanout 名单 = 有测试以 ``ginno_runtime.api.sessions.<name>`` 为 patch
    面、且真实调用点在资源模块命名空间内的名字。
    """

    _FANOUT: dict = {
        # —— 函数/常量 patch（真实调用点在资源模块命名空间）——
        "build_model": (_base, meta),  # 40+ 测试 patch/直接赋值（conftest fixture ×全量 e2e）
        "GOAL_GRACE_S": (goal,),  # e2e test_goal_continuation ×4
        "_ensure_session": (goal, delegations),  # lastwords/remote/subagent 等 ×20
        "_turn_last_error": (goal,),  # unit test_scheduler ×3（driver 内部调用）
        "_emit_goal_event": (goal, delegations),  # test_goal_set_steers_running（端点内部调用）
        "_start_goal_driver": (goal,),  # 同上
        "_first_agent_id": (goal,),  # _run_goal_turn 内部解析
        "_run_goal_turn": (goal,),  # driver loop 内部调用
        # —— 容器整体替换（test_goal_set_steers_running._wire 直接换新 dict）——
        "_SESSIONS": (_base, crud, meta, goal, relations, delegations),
        "_RUNNING_TURNS": (goal, delegations),
        "_GOAL_DRIVERS": (goal,),
    }

    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        for _mod in self._FANOUT.get(name, ()):
            setattr(_mod, name, value)


_sys.modules[__name__].__class__ = _PatchFanoutModule
