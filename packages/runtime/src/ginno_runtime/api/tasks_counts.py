"""轻量计数路径 ``GET /api/tasks/counts`` —— 常驻徽标的六源计数。

与 ``tasks_overview`` 的分工（2026-11-21 性能修复）：

* ``/api/tasks/overview``：完整枚举（句柄 + 标题 join + 进度 detail），面板
  打开时按 30s 轮询走这里——它要做 ``_find_meta`` 逐句柄扫索引和
  ``list_runs``/``all_active_plans`` 全历史 JSON 解析，属于「打开面板才付」
  的成本；
* ``/api/tasks/counts``：右栏「运行」tab 徽标常驻
  轮询的轻量路径——**只回计数、不做标题 join、不建句柄**，且 runs/plans/
  goals 走 mtime 增量索引（见 ``_PerFileIndex``）：历史文件增多后，周期轮询
  只付 O(N) 次 ``stat``（微秒级），JSON 解析只发生在「新文件 / 文件被重写」
  上；重启 = 空索引冷启动，首请求全量重建一次（兼顾重启重建）。

一致性纪律（计数 ⇆ 明细）：

* turns / delegations：与 overview 同一内存真源、同一过滤口径
  （``_RUNNING_TURNS ∪ _PENDING_RESUME`` 去重；委托表 ``proc is not None``
  才算 in-flight，预占槽不算）——在事件循环上做快照读取（纯内存，微秒级）；
* scheduled / subagents / runs / goals：与 overview 同一批磁盘文件、同一
  过滤字段（``status == "running"``、``status in PLAN_OPEN``、goal
  ``STATUS_ACTIVE``、调度全局/任务级 enabled），只是把「全量解析」换成
  「sig 未变复用缓存值」。文件状态变化必然 bump mtime/size（所有写入都走
  temp+rename 或整文件重写）→ 下一请求即失效重解析，不需要写路径挂钩。

事件循环纪律：端点是 async；一切磁盘 I/O（schedule config、四类索引刷新）
在 ``asyncio.to_thread`` 里跑，徽标轮询不再阻塞并发请求与 WS 帧。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter

from .. import paths, schedule_store, server_shared
from ..goals import store as goal_store
from ..plans import store as plan_store
from ..workflows import store as wf_store

router = APIRouter()

# 解析入口做了一层间接（``_loads``）而非直呼 ``json.loads``：单测靠 monkeypatch
# 这个名字统计「本轮刷新真实发生了几次 JSON 解析」，验证增量索引不重解析未变
# 文件（读取次数回归钉）。全局 json.loads 不能 patch——FastAPI 自己也用。
_loads = json.loads


def _stat_sig(st: os.stat_result) -> tuple[int, int]:
    """文件签名：mtime_ns + size。所有写路径都是整文件重写/原子替换，
    状态变化必然至少改其一；两个都未变 ⇒ 解析结果不会变。"""
    return (st.st_mtime_ns, st.st_size)


class _PerFileIndex:
    """``path → (sig, value)`` 增量缓存；``refresh`` 返回全部当前文件的 value。

    语义约定：

    * sig 未命中（新文件 / mtime 或 size 变化）→ 重新解析并缓存；
    * ``stat`` 失败（文件刚被删）→ 条目出索引、不产出 value；
    * 解析抛 ``OSError``/``ValueError``（坏 JSON）→ value 记 None 落缓存——
      与 overview 侧 list_runs/``_read_json``/plan/goal 的「坏文件跳过」同语义，
      且避免坏文件每轮询都被重新解析；
    * 目录里消失的文件 → 出索引（counts 随删即降，不需要失效钩子）。
    """

    def __init__(self, parse: Callable[[Path, str], Any]) -> None:
        self._parse = parse
        self._entries: dict[Path, tuple[tuple[int, int], Any]] = {}

    def refresh(self, files: Iterable[Path]) -> list[Any]:
        values: list[Any] = []
        seen: set[Path] = set()
        for p in files:
            seen.add(p)
            try:
                sig = _stat_sig(p.stat())
            except OSError:
                self._entries.pop(p, None)
                continue
            hit = self._entries.get(p)
            if hit is not None and hit[0] == sig:
                values.append(hit[1])
                continue
            try:
                value = self._parse(p, p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = None
            self._entries[p] = (sig, value)
            values.append(value)
        for stale in set(self._entries) - seen:
            self._entries.pop(stale, None)
        return values

    def clear(self) -> None:
        """重启重建语义的测试入口：清空后下一次 refresh 即冷启动全量解析。"""
        self._entries.clear()


# ---- 文件枚举（与 overview 侧各 store 的扫描范围逐一对齐） ------------------


def _run_files() -> list[Path]:
    # 真源：workflows.store._runs_dir()（全局 ~/.ginno/workflow_runs/<id>.json）。
    d = wf_store._runs_dir()
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _plan_files() -> list[Path]:
    # 真源：plans.store.all_active_plans() 的扫描范围
    # （projects/<slug>/sessions/<sid>/plans/*.json）。
    root = plan_store._projects_root()
    return sorted(root.glob("*/sessions/*/plans/*.json")) if root.is_dir() else []


def _session_index_files() -> list[Path]:
    # _find_meta 的扫描范围（projects/*/sessions/_index.json）——counts 不做
    # 标题 join，但 plans 计数要复刻 overview 的「会话已删则不枚举」过滤。
    return sorted(paths.home().glob("projects/*/sessions/_index.json"))


def _goal_files() -> list[Path]:
    # overview._collect_goals 同款 glob（projects/<slug>/goals.json）。
    return sorted(paths.home().glob("projects/*/goals.json"))


# ---- 四类索引（模块级单例；进程生命周期 = 缓存生命周期） --------------------


def _parse_run(_p: Path, text: str) -> int:
    # list_runs(status="running") 的过滤字段：顶层 "status" == "running"。
    data = _loads(text)
    return 1 if isinstance(data, dict) and data.get("status") == "running" else 0


def _parse_plan(p: Path, text: str) -> tuple[str, bool]:
    # 返回 (session_id, is_open)；is_open 口径 = PLAN_OPEN（active/pending_confirm）。
    # session_id 以 JSON 字段为准（create_plan 落盘），缺省回退目录名
    # （.../sessions/<sid>/plans/<plan_id>.json）——与 all_active_plans 的
    # sdir.name 同源。
    data = _loads(text)
    if not isinstance(data, dict):
        return ("", False)
    sid = str(data.get("session_id") or p.parent.parent.name)
    return (sid, data.get("status") in plan_store.PLAN_OPEN)


def _parse_session_ids(_p: Path, text: str) -> frozenset[str]:
    data = _loads(text)
    if not isinstance(data, list):
        return frozenset()
    return frozenset(str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id"))


def _parse_goal(_p: Path, text: str) -> int:
    # list_goals(slug) → 只数 STATUS_ACTIVE（与 overview._collect_goals 同口径）。
    data = _loads(text)
    if not isinstance(data, dict):
        return 0
    return sum(
        1
        for g in data.values()
        if isinstance(g, dict) and g.get("status") == goal_store.STATUS_ACTIVE
    )


_RUN_IDX = _PerFileIndex(_parse_run)
_PLAN_IDX = _PerFileIndex(_parse_plan)
_SESSION_IDX = _PerFileIndex(_parse_session_ids)
_GOAL_IDX = _PerFileIndex(_parse_goal)


def reset_counts_cache_for_tests() -> None:
    """模拟重启：清空全部增量索引（仅测试调用）。"""
    for idx in (_RUN_IDX, _PLAN_IDX, _SESSION_IDX, _GOAL_IDX):
        idx.clear()


# ---- 计数器 -----------------------------------------------------------------


def _count_turns() -> int:
    """内存快照（事件循环上读，无磁盘）：running ∪ parked，同会话去重——
    overview._collect_turns 的 seen 去重 + parked 覆盖语义在计数上就是
    集合并集的大小。"""
    return len(set(server_shared._RUNNING_TURNS) | set(server_shared._PENDING_RESUME))


def _count_phases(total: int, scheduled: int, waiting: int) -> dict[str, int]:
    """三档分类计数（2026-11-21，与 overview 的 handle.phase 同口径）的
    O(1) 推导，零额外 I/O：

    * waiting = ``|_PENDING_RESUME|``——overview 侧每个 parked sid 恰好产出
      一个 phase=waiting 的句柄（并集去重保证同 sid 只出现一次）；
    * running = total − waiting − scheduled——waiting 只出自 turns 组、
      scheduled 只出自 schedule 组，其余五组与 running turn 全是 running；
    * scheduled 即 scheduled 组计数（磁盘源已有）。

    ``total`` 传六组合计（含 scheduled 自己），不是 turns 单组计数。
    """
    return {
        "running": max(0, total - waiting - scheduled),
        "waiting": waiting,
        "scheduled": scheduled,
    }


def _count_delegations() -> int:
    """内存快照：in-flight 真源 _ACTIVE_DELEGATIONS，proc=None 的并发闸预占槽
    不算（overview._collect_delegations 同口径）。"""
    from ..tools.external_agent import _ACTIVE_DELEGATIONS, _ACTIVE_LOCK

    with _ACTIVE_LOCK:
        return sum(1 for e in _ACTIVE_DELEGATIONS.values() if e.get("proc") is not None)


def _count_scheduled() -> int:
    """启用的调度任务数（全局 enabled + 任务级 enabled；全局关 = 0）。
    单个小 JSON 文件，不值得索引，直接读（在线程里）。"""
    cfg = schedule_store.load_config()
    if not cfg.get("enabled", True):
        return 0
    return sum(1 for task in cfg.get("tasks") or [] if task.get("enabled"))


def _collect_disk_counts() -> dict[str, int]:
    """四类磁盘源的计数（worker 线程里跑，绝不进事件循环）。

    坏文件在索引里记 None（与 overview 侧「坏文件跳过」同语义），各 reduce
    都按 ``isinstance`` 防御，None 不产出计数。"""
    with _INDEX_LOCK:
        return _refresh_disk_counts()


_INDEX_LOCK = threading.Lock()


def _refresh_disk_counts() -> dict[str, int]:
    scheduled = _count_scheduled()
    session_values = _SESSION_IDX.refresh(_session_index_files())
    session_ids = frozenset().union(
        *(v for v in session_values if isinstance(v, frozenset))
    )
    subagents = sum(
        1
        for v in _PLAN_IDX.refresh(_plan_files())
        if isinstance(v, tuple) and v[1] and v[0] in session_ids
    )
    runs = sum(v for v in _RUN_IDX.refresh(_run_files()) if isinstance(v, int))
    goals = sum(v for v in _GOAL_IDX.refresh(_goal_files()) if isinstance(v, int))
    return {
        "scheduled": scheduled,
        "subagents": subagents,
        "runs": runs,
        "goals": goals,
    }


@router.get("/api/tasks/counts")
async def tasks_counts() -> dict:
    """六源计数（``counts`` 形状与 ``/api/tasks/overview`` 的 ``counts`` 一致）。

    内存源（turns/delegations）在事件循环上快照；磁盘源（scheduled/subagents/
    runs/goals）丢线程池——徽标 30s 轮询 never blocks the loop。
    """
    turns = _count_turns()
    waiting = len(server_shared._PENDING_RESUME)
    delegations = _count_delegations()
    disk = await asyncio.to_thread(_collect_disk_counts)
    counts: dict[str, Any] = {
        "turns": turns,
        "scheduled": disk["scheduled"],
        "subagents": disk["subagents"],
        "runs": disk["runs"],
        "goals": disk["goals"],
        "delegations": delegations,
    }
    counts["total"] = sum(counts.values())
    counts["phases"] = _count_phases(counts["total"], disk["scheduled"], waiting)
    return {"ok": True, "counts": counts}
