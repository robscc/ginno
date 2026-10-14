"""AgentTeam 模式（agent-team-design.md）：把 Agent 从「一份配置」实体化为
「有 session、有状态、有记忆、可被 @ 点名」的团队成员。

三个子模块，职责不重叠：

* :mod:`.store`   — 编制持久化（团队 / 成员实例 CRUD，锁 + 原子落盘）
* :mod:`.status`  — 语义态聚合 + 上卷（**唯一权威来源**，§5.9）
* :mod:`.deliver` — @ 投递 + 回帖（三条 herdr 硬纪律，§5.3.3）
* :mod:`.memory`  — 记忆分层（TEAM.md + 实例 notes.md 注入，§5.4）
* :mod:`.wait`    — ``list_members(wait=True)`` 的阻塞等待（§5.3.2，读 status）

Import 纪律：本包在 ``graph`` / ``tools`` 之下（deliver 要用 subagent 调度器
的 ``_run_managed_turn``），所以 api.sessions / api.stream / subagent_scheduler
一律**函数内懒导入**，避免 import 环——与 subagent_scheduler 同款约定。
"""

from __future__ import annotations

from . import deliver, memory, status, store, wait

__all__ = ["store", "status", "deliver", "memory", "wait"]
