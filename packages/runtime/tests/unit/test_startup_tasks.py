"""startup_tasks 守卫测试（批次 7：server.py lifespan 启动任务表化）。

三道闸：

1. **任务表快照** — STARTUP/SHUTDOWN 两表的 ``(name, fail_policy,
   depends_on)`` 全量钉死。name 序 == server.py 原 lifespan 平铺块
   执行顺序（等价性契约：表序即执行序，不重排）。任何增删/重排都会
   在这里红，并要求同步更新下面的原块对照清单。
2. **fail_policy 三态语义** — 合成表验证执行器：``abort`` 中断并
   re-raise；``continue`` 记日志后继续；``timeout_s`` 生效（超时取消
   任务体并按 fail_policy 分流；同步任务体不进 wait_for，闸为声明性
   预算——见 test_sync_body_timeout_is_declared_only）。
3. **表结构守卫** — phase 匹配所在表、depends_on 只指向同表更早的
   name、fail_policy 取值合法。

外加 P1-2 的 **timeout 闸守卫**：全表 timeout 恰为「快照 ∪ 白名单」
（test_real_table_timeout_gates）。
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from ginno_runtime import startup_tasks as st
from ginno_runtime.startup_tasks import (
    FAIL_ABORT,
    FAIL_CONTINUE,
    PHASE_SHUTDOWN,
    PHASE_STARTUP,
    PhaseTask,
    execute_phase,
)

# --------------------------------------------------------------------------- #
# 1. 任务表快照 + 与原 server.py 平铺块的一一对应清单
# --------------------------------------------------------------------------- #
# 等价性对照（原块序 == 表序；「原块」指 bef0a51 server.py lifespan 内的
# 平铺块，顺序即执行顺序）：
#
#   startup（36 项）:
#   1  ensure_layout            paths.ensure_layout()（无 try → abort）
#   2  startup_maintenance      try/except (continuing)
#   3  search_index_build       try/except (continuing)，起 daemon 线程
#   4  startup_check            try/except (continuing)
#   5  delegation_reconcile     外层 try/except pass + 3 个嵌套 backfill try
#                               （整块保留为单任务：reconcile 失败短路 backfill）
#   6  plan_watcher_recovery    try/except (continuing)
#   7  team_recovery            try/except (continuing)，await recover_teams()
#   8  i18n_health_check        try/except (continuing)
#   9  set_main_loop            无 try → abort
#   10 anyio_thread_limiter     try/except (continuing)（与 11 原为一个 try，
#                               2026-11-15 修复后已拆两个独立 try）
#   11 asyncio_default_executor try/except (continuing)
#   12 requeue_pending_titles   try/except (continuing)；注释明说在
#                               set_main_loop 之后 → depends_on
#   13 apply_system_proxy       try/except (continuing)
#   14 ensure_turn_log          无 try → abort
#   15 usage_cleanup            try/except (continuing)
#   16 memory_sweep             try/except (continuing)
#   17 session_files_migration  try/except (continuing)
#   18 hooks_init               无 try → abort（shared._hooks 赋值）
#   19 mods_classic_hooks       try/except (continuing)；读 18 的 _hooks → depends_on
#   20 todo_seed                无 try → abort
#   21 connector_seed           try/except (continuing)
#   22 browser_connector_setup  try/except (continuing)
#   23 agents_registry_seed     七连 ensure_*（无 try → abort）
#   24 web_permissions_migration try/except (continuing)
#   25 workflows_seed           无 try → abort
#   26 subagent_type_seed       try/except (continuing)
#   27 subagent_reconcile       try/except (continuing)
#   28 run_reconciliation       try/except (continuing)
#   29 refresh_session_metas    无 try → abort
#   30 mcp_registry_init        MCPRegistry()+load()（无 try → abort）
#   31 mcp_background_connect   create_task（无 try → abort）；读 30 的
#                               shared._mcp → depends_on
#   32 scheduler_start          try/except (continuing)，内层 cleanup try 保留
#   33 tasks_registry_start     try/except (continuing)
#   34 devswap_watch            create_task（无 try → abort）
#   35 shell_watch              create_task（无 try → abort）
#   36 lastwords_recover        create_task（无 try → abort）
#
#   shutdown（10 项，原 finally 段平铺序）:
#   S-1  lastwords_mark_shutdown  try/except pass
#   S-2  cancel_background_tasks  4 个 task cancel（无 try → abort）
#   S-3  lastwords_write_signal   try/except (continuing)
#   S-4  scheduler_stop           if scheduler_task: try/except（内层 try 保留）
#   S-5  run_tasks_shutdown       try/except (continuing)
#   S-6  synth_tasks_shutdown     try/except (continuing)
#   S-7  tasks_registry_stop      if started: try/except（内层 try 保留）
#   S-8  browser_profile_shutdown try/except (continuing)
#   S-9  mcp_close_all            if shared._mcp: await close_all()（无 try → abort）
#   S-10 mods_channel_shutdown    try/except (continuing)

STARTUP_SNAPSHOT: list[tuple[str, str, tuple[str, ...]]] = [
    ("ensure_layout", FAIL_ABORT, ()),
    ("startup_maintenance", FAIL_CONTINUE, ()),
    ("search_index_build", FAIL_CONTINUE, ()),
    ("startup_check", FAIL_CONTINUE, ()),
    ("delegation_reconcile", FAIL_CONTINUE, ()),
    ("plan_watcher_recovery", FAIL_CONTINUE, ()),
    ("team_recovery", FAIL_CONTINUE, ()),
    ("i18n_health_check", FAIL_CONTINUE, ()),
    ("set_main_loop", FAIL_ABORT, ()),
    ("anyio_thread_limiter", FAIL_CONTINUE, ()),
    ("asyncio_default_executor", FAIL_CONTINUE, ()),
    ("requeue_pending_titles", FAIL_CONTINUE, ("set_main_loop",)),
    ("apply_system_proxy", FAIL_CONTINUE, ()),
    ("ensure_turn_log", FAIL_ABORT, ()),
    ("usage_cleanup", FAIL_CONTINUE, ()),
    ("memory_sweep", FAIL_CONTINUE, ()),
    ("session_files_migration", FAIL_CONTINUE, ()),
    ("hooks_init", FAIL_ABORT, ()),
    ("mods_classic_hooks", FAIL_CONTINUE, ("hooks_init",)),
    ("todo_seed", FAIL_ABORT, ()),
    ("connector_seed", FAIL_CONTINUE, ()),
    ("browser_connector_setup", FAIL_CONTINUE, ()),
    ("agents_registry_seed", FAIL_ABORT, ()),
    ("web_permissions_migration", FAIL_CONTINUE, ()),
    ("workflows_seed", FAIL_ABORT, ()),
    ("subagent_type_seed", FAIL_CONTINUE, ()),
    ("subagent_reconcile", FAIL_CONTINUE, ()),
    ("run_reconciliation", FAIL_CONTINUE, ()),
    ("refresh_session_metas", FAIL_ABORT, ()),
    ("mcp_registry_init", FAIL_ABORT, ()),
    ("mcp_background_connect", FAIL_ABORT, ("mcp_registry_init",)),
    ("scheduler_start", FAIL_CONTINUE, ()),
    ("tasks_registry_start", FAIL_CONTINUE, ()),
    ("devswap_watch", FAIL_ABORT, ()),
    ("shell_watch", FAIL_ABORT, ()),
    ("lastwords_recover", FAIL_ABORT, ()),
]

SHUTDOWN_SNAPSHOT: list[tuple[str, str, tuple[str, ...]]] = [
    ("lastwords_mark_shutdown", FAIL_CONTINUE, ()),
    ("cancel_background_tasks", FAIL_ABORT, ()),
    ("lastwords_write_signal", FAIL_CONTINUE, ()),
    ("scheduler_stop", FAIL_CONTINUE, ("cancel_background_tasks",)),
    ("run_tasks_shutdown", FAIL_CONTINUE, ()),
    ("synth_tasks_shutdown", FAIL_CONTINUE, ()),
    ("tasks_registry_stop", FAIL_CONTINUE, ("cancel_background_tasks",)),
    ("browser_profile_shutdown", FAIL_CONTINUE, ()),
    ("mcp_close_all", FAIL_ABORT, ()),
    ("mods_channel_shutdown", FAIL_CONTINUE, ()),
]


def _as_snapshot(tasks: tuple[PhaseTask, ...]) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(t.name, t.fail_policy, t.depends_on) for t in tasks]


def test_startup_table_snapshot():
    """name 序（即执行序）+ fail_policy + depends_on 全量钉死。"""
    assert _as_snapshot(st.STARTUP_TASKS) == STARTUP_SNAPSHOT


def test_shutdown_table_snapshot():
    assert _as_snapshot(st.SHUTDOWN_TASKS) == SHUTDOWN_SNAPSHOT


#: P1-2 逐任务加闸快照：有闸任务的 name → timeout_s。改值 = 有意识的
#: 决策，必须同步本快照（档位理由见 startup_tasks.py STARTUP_TASKS
#: 前注释）。
TIMEOUT_SNAPSHOT: dict[str, float] = {
    "ensure_layout": 60.0,
    "startup_maintenance": 60.0,
    "search_index_build": 120.0,
    "startup_check": 60.0,
    "delegation_reconcile": 60.0,
    "plan_watcher_recovery": 60.0,
    "team_recovery": 60.0,
    "i18n_health_check": 60.0,
    "set_main_loop": 60.0,
    "anyio_thread_limiter": 60.0,
    "asyncio_default_executor": 60.0,
    "requeue_pending_titles": 120.0,
    "apply_system_proxy": 60.0,
    "ensure_turn_log": 60.0,
    "usage_cleanup": 60.0,
    "memory_sweep": 120.0,
    "session_files_migration": 120.0,
    "hooks_init": 60.0,
    "mods_classic_hooks": 60.0,
    "todo_seed": 60.0,
    "connector_seed": 60.0,
    "agents_registry_seed": 60.0,
    "web_permissions_migration": 60.0,
    "workflows_seed": 60.0,
    "subagent_type_seed": 60.0,
    "subagent_reconcile": 60.0,
    "run_reconciliation": 60.0,
    "refresh_session_metas": 300.0,
    "mcp_registry_init": 60.0,
    "scheduler_start": 60.0,
    "tasks_registry_start": 60.0,
    "lastwords_recover": 60.0,
}

#: 明确不加闸的任务白名单：name → 理由（startup_tasks.py 同款注释）。
UNTIMED_WHITELIST: dict[str, str] = {
    "browser_connector_setup": (
        "写扩展/native host 到用户目录，半安装态误杀排障成本高（best-effort）"
    ),
    "mcp_background_connect": (
        "慢 MCP 连接有 per-server timeout 自身语义；任务体仅 create_task"
    ),
    "devswap_watch": "watchdog 型长驻 watcher，闸只会误杀；任务体仅 create_task",
    "shell_watch": "watchdog 型长驻 watcher，闸只会误杀；任务体仅 create_task",
    # shutdown 全表：uvicorn timeout_graceful_shutdown=3s 是真正的关闭
    # 预算；执行器层闸要么永不触发要么抢跑优雅收尾。
    "lastwords_mark_shutdown": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "cancel_background_tasks": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "lastwords_write_signal": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "scheduler_stop": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "run_tasks_shutdown": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "synth_tasks_shutdown": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "tasks_registry_stop": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "browser_profile_shutdown": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "mcp_close_all": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
    "mods_channel_shutdown": "shutdown 表整体不加闸（uvicorn 3s 窗口）",
}


def test_real_table_timeout_gates():
    """P1-2 加闸守卫：全表 timeout 恰为「快照 ∪ 白名单」。

    双断言语义（dict 相等一举覆盖）：
    * 有闸任务的值与 TIMEOUT_SNAPSHOT 逐项一致（改值必须有意识同步）；
    * 无闸任务恰为 UNTIMED_WHITELIST——新增任务不配闸会红（逼一次
      归档决策：进快照还是进白名单+理由），白名单任务被偷加闸也会红。
    """
    actual = {t.name: t.timeout_s for t in st.STARTUP_TASKS + st.SHUTDOWN_TASKS}
    expected = dict(TIMEOUT_SNAPSHOT)
    expected.update({name: None for name in UNTIMED_WHITELIST})
    assert actual == expected


def test_table_structure_invariants():
    """phase 匹配所在表；depends_on 只指向同表更早的 name；fail_policy 合法。"""
    for table, phase in (
        (st.STARTUP_TASKS, PHASE_STARTUP),
        (st.SHUTDOWN_TASKS, PHASE_SHUTDOWN),
    ):
        seen: set[str] = set()
        names = [t.name for t in table]
        assert len(names) == len(set(names)), "duplicate task names"
        for t in table:
            assert t.phase == phase, t.name
            assert t.fail_policy in (FAIL_CONTINUE, FAIL_ABORT), t.name
            for dep in t.depends_on:
                assert dep in seen, f"{t.name} depends on later/absent {dep}"
            seen.add(t.name)


# --------------------------------------------------------------------------- #
# 2. fail_policy 三态语义（合成表，不触碰真实副作用）
# --------------------------------------------------------------------------- #

def _mk(name, run, fail_policy=FAIL_CONTINUE, depends_on=(), timeout_s=None):
    return PhaseTask(
        name=name,
        phase=PHASE_STARTUP,
        run=run,
        fail_policy=fail_policy,
        depends_on=depends_on,
        timeout_s=timeout_s,
    )


async def test_abort_interrupts_and_reraises():
    """abort 失败：原异常向上传播，后续任务一律不执行。"""
    order: list[str] = []

    def boom(_h):
        raise RuntimeError("boom-abort")

    table = (
        _mk("first_ok", lambda _h: order.append("first_ok")),
        _mk("aborts", boom, fail_policy=FAIL_ABORT),
        _mk("never", lambda _h: order.append("never")),
    )
    with pytest.raises(RuntimeError, match="boom-abort"):
        await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert order == ["first_ok"]


async def test_continue_logs_and_keeps_going():
    """continue 失败：不抛、后续任务照常执行（执行器记 exception 日志）。"""
    order: list[str] = []

    def boom(_h):
        raise RuntimeError("boom-continue")

    async def ok(_h):
        order.append("after-failure")

    table = (
        _mk("continues", boom, fail_policy=FAIL_CONTINUE),
        _mk("second", ok),
    )
    done = await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert order == ["after-failure"]
    assert done == ["continues", "second"]


async def test_timeout_cancels_and_continues():
    """timeout_s 生效：超时取消任务体，continue 策略下后续任务照跑。"""
    order: list[str] = []
    cancelled = asyncio.Event()

    async def slow(_h):
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def after(_h):
        order.append("after-timeout")

    table = (
        _mk("slow", slow, timeout_s=0.05),
        _mk("after", after),
    )
    done = await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert order == ["after-timeout"]
    assert done == ["slow", "after"]
    assert cancelled.is_set(), "timed-out body must be cancelled, not left running"


async def test_timeout_with_abort_propagates():
    """timeout + abort：TimeoutError 原样向上传播并中断。"""

    async def slow(_h):
        await asyncio.sleep(30.0)

    table = (_mk("slow", slow, fail_policy=FAIL_ABORT, timeout_s=0.05),)
    with pytest.raises(asyncio.TimeoutError):
        await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())


async def test_sync_body_timeout_is_declared_only():
    """已知限制钉住：wait_for 只包 awaitable——同步任务体（真实表多数
    形态）直接在 event loop 里跑，timeout_s 不会被触发，是声明性耗时
    预算（任务体 async 化后闸自动生效）。本用例防止未来有人以为同步
    任务也能被超时取消而依赖该行为。"""

    ran: list[str] = []

    def blocking(_h):
        time.sleep(0.05)  # 超过 timeout_s，但同步体不经 wait_for
        ran.append("sync-done")

    table = (_mk("blocking", blocking, timeout_s=0.01),)
    done = await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert done == ["blocking"]
    assert ran == ["sync-done"]


async def test_real_async_tasks_have_live_gates():
    """真实表中 async 任务体的闸是**可真实触发**的（wait_for 真包裹）——
    其余同步任务的 timeout_s 为声明性预算。本用例锁住「谁拥有活闸」，
    防止 async 化/同步化静默改变闸的语义而不被察觉。"""
    async_bodies = {
        t.name
        for t in st.STARTUP_TASKS
        if inspect.iscoroutinefunction(t.run)
    }
    assert async_bodies == {"team_recovery", "requeue_pending_titles"}, (
        "真实表 async 任务集变化：新 async 任务拥有可真实触发的闸，"
        "请同步本断言并复核其 timeout 档位"
    )
    for name in async_bodies:
        task = next(t for t in st.STARTUP_TASKS if t.name == name)
        assert task.timeout_s is not None, f"{name} 是 async 体，必须有活闸"


async def test_sync_and_async_callbacks_both_supported():
    """同一表混合同步/异步回调（真实表形态），执行器都要跑。"""
    order: list[str] = []

    async def an_async(_h):
        await asyncio.sleep(0)
        order.append("async")

    table = (
        _mk("sync", lambda _h: order.append("sync")),
        _mk("async", an_async),
    )
    await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert order == ["sync", "async"]


async def test_depends_on_guard_raises_on_missing_dep():
    """声明依赖未在更早表位完成 → RuntimeError（表 bug 守卫）。"""
    table = (
        _mk("a", lambda _h: None),
        _mk("b", lambda _h: None, depends_on=("missing",)),
    )
    with pytest.raises(RuntimeError, match="depends_on"):
        await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())


async def test_phase_mismatch_rejected():
    """phase 与表不匹配（shutdown 任务混进 startup 表）→ RuntimeError。"""
    bad = PhaseTask(
        name="misplaced",
        phase=PHASE_SHUTDOWN,
        run=lambda _h: None,
    )
    with pytest.raises(RuntimeError, match="phase"):
        await execute_phase((bad,), PHASE_STARTUP, st.LifespanHandles())


async def test_continued_task_counts_as_completed_for_deps():
    """continue 失败的依赖仍算完成——原平铺代码失败后照样顺序往下走。"""
    order: list[str] = []

    def boom(_h):
        raise RuntimeError("dep failed")

    async def dependent(_h):
        order.append("dependent-ran")

    table = (
        _mk("dep", boom, fail_policy=FAIL_CONTINUE),
        _mk("dependent", dependent, depends_on=("dep",)),
    )
    await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert order == ["dependent-ran"]
