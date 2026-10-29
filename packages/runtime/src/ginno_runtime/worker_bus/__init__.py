"""Worker protocol bus（docs/worker-protocol-design.md）——跨 session 的
派活与订阅协议，独立于 AgentTeam（不 import ``teams/*``、不依赖
TEAM.md/成员实例）。

复用的全部是**引擎级**原语：

* 投递双路：steer stash（忙 → 同 turn 吸收）+ wake turn（闲 → 新回合），
  即 ``subagent_scheduler.runner`` 的注入管道（``_inject_result`` /
  ``_wake_parent_turn``，后者带出口码返回）；
* 状态与花名册：``session_meta`` 的磁盘索引（``_index.json``），
  不新建存储；
* 订阅注册：目标 meta 的 ``subscribers[]`` 字段（``session_meta``
  串行锁保证跨 session 写安全）；
* 信封：注入消息的 additional_kwargs（``ginno_worker_msg`` 溯源键）
  + 文本层 XML 信封（``<ginno_worker_assign>`` / ``<ginno_worker_result>``，
  前端折卡片，同 ``<ginno_subagent_result>`` 契约）。

Import discipline：本模块被 ``graph.build_all_tools`` 拉起（经
``worker_bus.tools``），位于 api.* 之下——api.stream / api.sessions 一律
在函数内 lazy import。
"""

from __future__ import annotations

from .registry import (
    ASSIGNABLE_TYPES,
    TASK_TYPES,
    TERMINAL_PROJECTED,
    list_worker_rows,
    project_status,
    resolve_target,
)
from .deliver import assign_to_worker
from .watch import (
    DIGEST_MIN_INTERVAL_S,
    SUBSCRIBER_CAP,
    notify_settled,
    on_turn_settled_hook,
    reconcile_subscriptions,
    watch_worker,
)

__all__ = [
    "ASSIGNABLE_TYPES",
    "TASK_TYPES",
    "TERMINAL_PROJECTED",
    "DIGEST_MIN_INTERVAL_S",
    "SUBSCRIBER_CAP",
    "assign_to_worker",
    "list_worker_rows",
    "notify_settled",
    "on_turn_settled_hook",
    "project_status",
    "reconcile_subscriptions",
    "resolve_target",
    "watch_worker",
]
