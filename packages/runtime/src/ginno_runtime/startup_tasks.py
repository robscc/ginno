"""Declarative startup/shutdown task table powering ``server.lifespan``.

server.py 的 lifespan 启动段原是约 330 行平铺块：十几个 ``try/except
(continuing)`` + 5 个 ``create_task``，任务间依赖靠隐含顺序假设。本模块
把它表化：

* :data:`STARTUP_TASKS` / :data:`SHUTDOWN_TASKS` — 声明表，每任务一个
  :class:`PhaseTask`（name / phase / run 回调 / fail_policy / depends_on /
  timeout_s）。**表序即执行序**（拓扑序退化为列表序，不重排——与原
  lifespan 平铺顺序逐块一一对应，见各任务体的 ``原块 N`` 注释）。
* :func:`execute_phase` — 通用执行器：按表序执行，``fail_policy``
  分流（``abort`` 失败立即 re-raise 中断本阶段；``continue`` 失败记
  exception 日志后继续），``timeout_s`` 非空时用
  :func:`asyncio.wait_for` 包裹（超时取消该任务体并按 fail_policy
  分流；真实表当前全部 None——行为与原平铺代码零差异，字段留给
  后续逐任务加闸）。

fail_policy 归类（保守，对应原代码形态）：

* 原块自带 ``try/except ... (continuing)`` 外壳 → ``continue``（外壳
  移交执行器统一承担；原块内**嵌套的** try/except 有独立语义与日志
  tag，逐字保留在任务体内不动）。
* 原块无 try 包裹（``paths.ensure_layout`` / ``set_main_loop`` /
  agents/workflows seed / MCP registry 等）→ ``abort``（失败原样向上
  传播；启动段 abort 时 lifespan 尚未进入 ``try:``，yield 后的
  shutdown 段不会执行——与原平铺语义一致）。

任务体迁自 server.py lifespan，逻辑逐字保留（仅有的适配：局部任务
句柄改挂 :class:`LifespanHandles`、facade 私有名改为从真源延迟
import）。``_watch_shell_pid`` / ``_connect_mcp_background`` 两个纯
lifespan 辅助随迁本模块，server.py 底部 facade 照旧 re-export。
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
from dataclasses import dataclass
from typing import Any, Callable

from . import agents as agents_reg
from . import paths, usage_store
from . import server_shared as shared
from . import workflows as wf_store
from .hooks.dispatcher import HookDispatcher
from .mcp.registry import MCPRegistry
from .server_shared import _ensure_turn_log, _log
from .todos import store as todo_store

PHASE_STARTUP = "startup"
PHASE_SHUTDOWN = "shutdown"

FAIL_CONTINUE = "continue"
FAIL_ABORT = "abort"

#: 执行器对 continue 任务失败时的统一日志格式。原平铺块各自的日志 tag
#: （如 ``startup_maintenance_failed (continuing)``）由任务 name 派生，
#: 文案有最小漂移（词间加空格），无任何测试/解析依赖该文案。
_FAIL_LOG_FMT = "%s failed (continuing)"


@dataclass
class LifespanHandles:
    """跨 startup/shutdown 两阶段传递的任务句柄（原 lifespan 局部变量）。

    shutdown 段只在 startup 全部走完后（yield 之后）才执行，届时各
    句柄必已赋值——与原局部变量生命周期一致。
    """

    mcp_connect_task: asyncio.Task | None = None
    scheduler_task: asyncio.Task | None = None
    tasks_registry_started: bool = False
    devswap_watch_task: asyncio.Task | None = None
    shell_watch_task: asyncio.Task | None = None
    lastwords_recover_task: asyncio.Task | None = None


@dataclass(frozen=True)
class PhaseTask:
    """一条声明式启动/关闭任务。

    :param name: 任务名（snake_case）；守卫测试钉死全表 name 序。
    :param phase: ``startup`` / ``shutdown``，须与所在表匹配（执行器校验）。
    :param run: 回调，签名 ``(handles) -> Awaitable | None``。同步任务
        返回 None（执行器跳过 await），异步任务返回 awaitable。
    :param fail_policy: ``continue``（失败记日志后继续）/ ``abort``
        （失败 re-raise 中断本阶段）。
    :param depends_on: 声明性依赖（仅守卫校验，不参与排序——表序即执行序）。
    :param timeout_s: 可选超时；非空时任务体被 :func:`asyncio.wait_for`
        包裹，超时即取消并按 fail_policy 分流。
    """

    name: str
    phase: str
    run: Callable[[LifespanHandles], Any]
    fail_policy: str = FAIL_CONTINUE
    depends_on: tuple[str, ...] = ()
    timeout_s: float | None = None


async def execute_phase(
    tasks: tuple[PhaseTask, ...], phase: str, handles: LifespanHandles
) -> list[str]:
    """按表序执行一个阶段的全部任务，返回完成 name 列表（表序）。

    :param phase: 本阶段期望值（startup / shutdown）；表内任何任务的
        ``phase`` 与之不符即 ``RuntimeError``（表 bug，两阶段同罪）。

    * 表序即执行序——不拓扑排序、不重排（等价性契约，守卫测试钉住）。
    * ``depends_on`` 仅作声明守卫：依赖未在更早的表位完成即
      ``RuntimeError``（表 bug，属于编程错误，两阶段同罪）。
    * ``abort`` 失败原样 re-raise（startup 段 = lifespan 向上抛、
      不进 yield/finally；shutdown 段 = finally 内抛出，替换在途
      异常——均与原平铺语义一致）。
    """
    completed: list[str] = []
    done: set[str] = set()
    for task in tasks:
        if task.phase != phase:
            raise RuntimeError(
                f"phase task {task.name!r} declares phase {task.phase!r} "
                f"in a {phase!r} execution"
            )
        missing = [dep for dep in task.depends_on if dep not in done]
        if missing:
            raise RuntimeError(
                f"phase task {task.name!r} depends_on {missing} not yet "
                "completed — list order IS the execution order; fix the "
                "table, do not reorder it."
            )
        try:
            outcome = task.run(handles)
            if inspect.isawaitable(outcome):
                if task.timeout_s is not None:
                    await asyncio.wait_for(outcome, task.timeout_s)
                else:
                    await outcome
        except Exception:
            if task.fail_policy == FAIL_ABORT:
                raise
            _log.exception(_FAIL_LOG_FMT, task.name)
        completed.append(task.name)
        done.add(task.name)
    return completed


# --------------------------------------------------------------------------- #
# 纯 lifespan 辅助（自 server.py 迁入，server.py 底部 facade re-export）
# --------------------------------------------------------------------------- #

async def _watch_shell_pid(poll_s: float = 2.0, fire=None) -> None:
    """壳 pid 看守（反孤儿化，2026-11-04）：壳硬死（崩溃/强退/kill -9）时
    sidecar 不随之消亡——孤儿 sidecar 占着 8787、后台命令全体陪跑（当日
    PID 60637 事故）。轮询 env GINNO_SHELL_PID（壳 spawn 时注入，prod/dev
    两分支都有；无 env = 人肉看管的裸 dev 服务，不启用），壳 pid 消失或本
    进程被过继（ppid 变化）即自请退出：给自己发 SIGTERM → uvicorn 优雅
    关闭（timeout_graceful_shutdown 兜底）→ lifespan finally 的 stop_all
    等一起收尾。``fire`` 参数仅测试注入。
    """
    raw = os.environ.get("GINNO_SHELL_PID", "")
    if not raw.isdigit() or int(raw) <= 1:
        return
    shell_pid = int(raw)
    ppid0 = os.getppid()
    while True:
        await asyncio.sleep(poll_s)
        gone = os.getppid() != ppid0  # 被过继 = 直接父（壳/uv wrapper）没了
        if not gone:
            try:
                os.kill(shell_pid, 0)
            except ProcessLookupError:
                gone = True
            except OSError:
                pass  # PermissionError 等：pid 复用为他户进程，保守视作活着
        if gone:
            _log.info("shell_gone_self_terminating shell_pid=%s", shell_pid)
            (fire or (lambda: os.kill(os.getpid(), signal.SIGTERM)))()
            return


async def _connect_mcp_background() -> None:
    try:
        await shared._mcp.connect_all()
    except Exception:
        _log.exception("background MCP connect_all failed")


async def _lastwords_recover_task() -> None:
    await asyncio.sleep(2.0)
    try:
        from .lastwords import recover_lastwords

        await recover_lastwords()
    except Exception:  # noqa: BLE001
        _log.exception("lastwords recovery failed (continuing)")


# --------------------------------------------------------------------------- #
# startup 任务体（迁自 server.py lifespan 启动段；「原块 N」= 原平铺顺序）
# --------------------------------------------------------------------------- #

def _task_ensure_layout(handles: LifespanHandles) -> None:
    # 原块 1（无 try → abort）。
    paths.ensure_layout()


def _task_startup_maintenance(handles: LifespanHandles) -> None:
    # 原块 2。数据卫生（app-iter-harness 04_ginno_ux 工单批，
    # maintenance.py）：孤儿 artifact、失效 workspace、无 agent 的历史
    # 会话、陈旧 last_error、重复标题。幂等 + best-effort —— 任何一步
    # 失败都不阻止启动。
    from .maintenance import run_startup_maintenance

    run_startup_maintenance()


def _task_search_index_build(handles: LifespanHandles) -> None:
    # 原块 3。P0-1 消息索引：后台增量构建，搜索在它完成前只匹配标题。
    import threading as _threading

    from . import search_index as _search_index

    _threading.Thread(
        target=_search_index.build_index, name="search-index", daemon=True
    ).start()


def _task_startup_check(handles: LifespanHandles) -> None:
    # 原块 4。P4-2 启动自检：轻量（不读 checkpoint、不发 HTTP），结果经
    # /api/health 暴露。
    from . import diagnostics

    diagnostics.startup_check()


def _task_delegation_reconcile(handles: LifespanHandles) -> None:
    # 原块 5（外层 try/except pass 整块原样保留——reconcile 失败会短路
    # 后三个 backfill，拆分会改语义）。
    # 委托子会话启动对账：上个进程的后台委托随重启消亡，running 态存量
    # 一律改判 error，避免侧栏出现永远转圈的委托行（2026-10-05）。
    try:
        from .delegation_sessions import reconcile_running_delegations

        reconcile_running_delegations()
        # type 主动标注的一次性迁移（2026-10-29）：历史 main 会话 type 缺省
        # 补写 "main"——worker 协议的可派活白名单不再靠缺省反推。幂等，
        # 收敛后每次启动零写入。
        try:
            from .session_meta import backfill_session_types

            n = backfill_session_types()
            if n:
                _log.info("session_type_backfill stamped=%d", n)
        except Exception:
            _log.exception("session_type_backfill_failed (continuing)")
        # Worker 协议订阅对账（worker-protocol-design §6.2）：崩溃在终态
        # 翻转与回传之间错过的订阅，启动时补发 + 重建 _WATCHED 快查表。
        try:
            from .worker_bus import reconcile_subscriptions

            reconcile_subscriptions()
        except Exception:
            _log.exception("worker_reconcile_failed (continuing)")
        # 关联会话戳对账（topbar-unification §3.2）：历史 watch 订阅在订阅方
        # 没有 linked_auto 截（戳只在新动作落）——补上,旧订阅才能进面板。
        try:
            from .session_links import backfill_linked_stamps

            n = backfill_linked_stamps()
            if n:
                _log.info("linked_stamps_backfilled=%d", n)
        except Exception:
            _log.exception("linked_stamps_backfill_failed (continuing)")
    except Exception:  # noqa: BLE001 — 启动绝不因对账失败而中断
        pass


def _task_plan_watcher_recovery(handles: LifespanHandles) -> None:
    # 原块 6。清单 watcher 恢复（subagent-plan-orchestration-design.md §6）：
    # 扫全部 open 清单——active 的会话起 watcher 接着跑（running 条目靠
    # tick 对账自动校准）；pending_confirm 等用户，不起。会话已删除的跳过。
    # plan_resume_on_restart=False（§6 开关）时改为取消恢复的 active 清单
    # ——不无人值守地续跑。
    from .plans import store as _plan_store
    from .plans import watcher as _plan_watcher
    from .session_meta import _find_meta
    from .world_state import plan_resume_on_restart

    _resume = plan_resume_on_restart()
    for _slug, _sid, _plan in _plan_store.all_active_plans():
        _meta, _ = _find_meta(_sid) or ({}, None)
        if not _meta or _plan.get("status") != _plan_store.PLAN_ACTIVE:
            continue
        if _resume:
            _plan_watcher.ensure_watcher(_sid)
        else:
            _plan_store.cancel_plan(_slug, _sid, _plan["plan_id"])
            _log.info(
                "plan_resume_disabled_cancelled session=%s plan=%s",
                _sid, _plan["plan_id"],
            )


async def _task_team_recovery(handles: LifespanHandles) -> None:
    # 原块 7。AgentTeam 恢复（2026-10-14）：存量领队回填 + 孤儿团队会话清理。
    from .api.teams import recover_teams

    await recover_teams()


def _task_i18n_health_check(handles: LifespanHandles) -> None:
    # 原块 8。i18n bundle check (i18n-design.md §10.5): both catalogs
    # loadable + key/placeholder parity. A frozen-app packaging gap (the
    # JSONs missing from the bundle) must surface HERE, not on first use —
    # same lesson as the zlib TOC incident. Log-only: degraded copy beats
    # a startup crash.
    from .i18n import i18n_health_check

    _i18n_problems = i18n_health_check()
    if _i18n_problems:
        _log.error("i18n_bundle invalid: %s", "; ".join(_i18n_problems))


def _task_set_main_loop(handles: LifespanHandles) -> None:
    # 原块 9（无 try → abort）。Record the main loop so sync (threadpool)
    # REST handlers can schedule WS broadcasts via run_coroutine_threadsafe
    # (spawn_bg) instead of failing with "no running event loop".
    shared.set_main_loop(asyncio.get_running_loop())


def _task_anyio_thread_limiter(handles: LifespanHandles) -> None:
    # 原块 10。2026-11-15 内存基线治理：线程池右 sizing。anyio worker 池
    # 默认上限 40（sync 端点 + 静态 FileResponse 走 run_in_threadpool，空闲
    # 不自退）+ asyncio 默认 executor min(32, cpu+4)=12 → 实测 43 线程。
    # 收敛到 12+8，栈虚拟内存减半、调度/诊断噪声下降；sync 端点并发上限
    # 显式化。
    #
    # ⚠️ 2026-11-15 修复：原写法 `anyio.current_default_thread_limiter()` 在
    # anyio 4.x 下**顶层不存在该属性**（实测 4.14.2：顶层 False、
    # anyio.to_thread 下 True），每次启动都抛 AttributeError 被 except 吞掉，
    # 只留一行 "right-sizing failed (continuing)"。后果不止 limiter 没改：
    # **同一个 try 块里后一行的 set_default_executor 也一起没执行**，两项
    # 优化全部落空，且不挡启动所以一直没被发现。故①改用正确路径
    # anyio.to_thread；②两项拆成独立 try（现为独立任务），一项失败不连带
    # 另一项。
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = 12


def _task_asyncio_default_executor(handles: LifespanHandles) -> None:
    # 原块 11（与原块 10 拆分独立，见上）。
    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=8, thread_name_prefix="ginno-io")
    )


async def _task_requeue_pending_titles(handles: LifespanHandles) -> None:
    # 原块 12。UX-03：标题生成是后台任务，进程重启会丢掉失败的 job——
    # 启动时把「待生成且超过 24h」的会话重新入队（assistant 文本从
    # checkpoint 回填）。放在 set_main_loop（原块 9）之后：生成任务经
    # spawn_bg 调度。
    from .maintenance import requeue_pending_titles

    await requeue_pending_titles()


def _task_apply_system_proxy(handles: LifespanHandles) -> None:
    # 原块 13。Apply the user's system-proxy switch (Settings → 模型 API)
    # before any LLM client exists: verify / sessions / probes all ride
    # httpx, whose clients freeze their proxy map at init. Default honours
    # OS proxies.
    from . import providers as _prov

    _prov.apply_system_proxy(_prov.use_system_proxy())


def _task_ensure_turn_log(handles: LifespanHandles) -> None:
    # 原块 14（无 try → abort）。Attach the trace-file handler up front so
    # pre-turn lifecycle lines (session_create / ws_open / ws_close) land
    # in the log, not just turn lines.
    _ensure_turn_log()


def _task_usage_cleanup(handles: LifespanHandles) -> None:
    # 原块 15。Drop usage logs past the retention window
    # (usage-stats-design.md §4.3). Best-effort and cheap (a directory
    # glob); never blocks startup on failure.
    usage_store.cleanup()


def _task_memory_sweep(handles: LifespanHandles) -> None:
    # 原块 16。Memory pipeline hygiene (docs/dream-mode-design.md §4.5):
    # fold the legacy single-slot draft into the queue, expire drafts
    # nobody reviewed, and trim the run log. A stalled draft used to block
    # every later distillation forever (2026-10-01 → 10-25); the TTL makes
    # that impossible.
    from .memory import expire_stale_drafts, migrate_legacy_draft, trim_runs

    migrate_legacy_draft()
    expire_stale_drafts()
    trim_runs()


def _task_session_files_migration(handles: LifespanHandles) -> None:
    # 原块 17。Best-effort, idempotent move of legacy session files into
    # their per-session dirs. Runs before `yield`, so nothing
    # (uploads/previews/watchers) can race it. Must never block startup on
    # failure.
    from . import migration as _migration

    _migration.migrate_session_files()


def _task_hooks_init(handles: LifespanHandles) -> None:
    # 原块 18（无 try → abort）。
    shared._hooks = HookDispatcher.from_settings()


def _task_mods_classic_hooks(handles: LifespanHandles) -> None:
    # 原块 19。Mods classic hooks bridging (claude-code-mods-design.md §10):
    # register every installed mod's classic-shape hooks.json into the
    # dispatcher (the JS "modules" shape rides the broker instead).
    # Best-effort.（依赖原块 18 建好的 shared._hooks。）
    from .mods.bridge_utils import register_classic_plugin_hooks

    register_classic_plugin_hooks(shared._hooks)


def _task_todo_seed(handles: LifespanHandles) -> None:
    # 原块 20（无 try → abort）。
    todo_store.ensure_seeded()


def _task_connector_seed(handles: LifespanHandles) -> None:
    # 原块 21。Connector registry (connector-module-design.md): seed the
    # built-in connectors so /api/connectors answers from the very first
    # request.
    from .connectors import ensure_builtin_connectors as _seed_connectors

    _seed_connectors()


def _task_browser_connector_setup(handles: LifespanHandles) -> None:
    # 原块 22。Browser connector setup (browser-companion design §7.3):
    # materialize the companion extension into ~/.ginno/browser-extension
    # (the folder the user loads via chrome://extensions), install the
    # native-messaging host manifests, and publish the relay port for the
    # host script. Best-effort.
    from .browser.native_host import (
        install_host,
        materialize_extension,
        write_port_file,
    )

    materialize_extension()
    install_host()
    write_port_file(int(os.environ.get("GINNO_RUNTIME_PORT", "8787")))


def _task_agents_registry_seed(handles: LifespanHandles) -> None:
    # 原块 23（无 try → abort）。
    agents_reg.ensure_todo_tools()
    agents_reg.ensure_research_discipline()
    agents_reg.ensure_goal_tools()
    agents_reg.ensure_web_tools()
    agents_reg.ensure_workflow_dev_tools()
    agents_reg.ensure_workflow_dev()
    agents_reg.ensure_workflow_dev_discipline()


def _task_web_permissions_migration(handles: LifespanHandles) -> None:
    # 原块 24。Upgraded installs never got the web tools in
    # permissions.allow (defaults seed only fresh homes) — migrate so they
    # don't fall through to `ask`.
    from .permission.policy import ensure_browser_permissions, ensure_web_permissions

    ensure_web_permissions()
    ensure_browser_permissions()


def _task_workflows_seed(handles: LifespanHandles) -> None:
    # 原块 25（无 try → abort）。
    wf_store.ensure_seeded()


def _task_subagent_type_seed(handles: LifespanHandles) -> None:
    # 原块 26。Subagent type registry seed (P3): an empty registry is
    # "no types defined", not a load failure — ship the default set so
    # agent_type works out of the box (same convention as the agents
    # registry seeds).
    from . import subagent_types

    subagent_types.ensure_seeded()


def _task_subagent_reconcile(handles: LifespanHandles) -> None:
    # 原块 27。Reconcile subagent metas left live (running/waiting) by the
    # previous process (subagent-design.md §5.7): their turn/wake/settle
    # tasks all died with it, and a ghost "running" meta would consume a
    # concurrency slot forever. Best-effort; never blocks startup.
    from .subagent_scheduler import reconcile_orphan_subagents

    reconcile_orphan_subagents()


def _task_run_reconciliation(handles: LifespanHandles) -> None:
    # 原块 28。Reconcile workflow runs left "running" by a previous
    # crash/quit: at this point no background task can be alive, so every
    # "running" run is an orphan that would otherwise stay stuck forever.
    # Best-effort; never blocks startup.
    from .api.workflows import _reconcile_orphan_runs

    _reconcile_orphan_runs()


def _task_refresh_session_metas(handles: LifespanHandles) -> None:
    # 原块 29（无 try → abort）。Heal session metas frozen with a
    # provider/model by older builds (or a config edited outside the UI) so
    # topbar + rebuilt graphs use current config.
    from .api.config import _refresh_session_metas

    _refresh_session_metas()


def _task_mcp_registry_init(handles: LifespanHandles) -> None:
    # 原块 30（无 try → abort）。
    shared._mcp = MCPRegistry()
    shared._mcp.load()


def _task_mcp_background_connect(handles: LifespanHandles) -> None:
    # 原块 31（无 try → abort）。Do NOT block port bind on MCP connections.
    # A slow/hung MCP server can take up to its per-server timeout, and the
    # HTTP/WS server (and thus the UI) must come up regardless. Connect in
    # the background; a session created before connections finish simply
    # starts without those tools (the /api/mcp/reload endpoint or a new
    # session picks them up once ready).
    handles.mcp_connect_task = asyncio.create_task(_connect_mcp_background())


def _task_scheduler_start(handles: LifespanHandles) -> None:
    # 原块 32（内层 cleanup try 有独立日志 tag，原样保留）。定时任务调度器
    # （scheduled-tasks-design.md §3.7）：启动对账（孤儿 running 行 →
    # error("interrupted")）+ 单例 30s tick 循环，随 lifespan cancel。
    from . import schedule_store as _sched_store
    from . import scheduler as _scheduler

    try:
        _sched_store.cleanup()
    except Exception:
        _log.exception("schedule cleanup failed (continuing)")
    handles.scheduler_task = _scheduler.start()


def _task_tasks_registry_start(handles: LifespanHandles) -> None:
    # 原块 33。后台任务注册表（background-tasks-design.md §7）：重启对账
    # （running → interrupted / detached 认领）+ 保留策略清理 + reaper 循环。
    from .tasks import get_registry as _get_tasks_registry
    from .tasks import store as _task_store

    _task_store.cleanup_expired(_task_store.read_index())
    _get_tasks_registry().start()
    handles.tasks_registry_started = True


def _task_devswap_watch(handles: LifespanHandles) -> None:
    # 原块 34（无 try → abort）。dev-swap 壳收尾终态 watcher（api/devswap.py）：
    # 重启后的新进程盯 dev-override/state.json 的 pending → verified/
    # rolled_back 跳变，补报 finish 终态（旧进程已被换掉，报不了这一笔）。
    from .api import devswap as _devswap_api

    handles.devswap_watch_task = asyncio.create_task(_devswap_api.watch_swap_finish())


def _task_shell_watch(handles: LifespanHandles) -> None:
    # 原块 35（无 try → abort）。壳 pid 看守（反孤儿化，见
    # _watch_shell_pid docstring）：壳硬死时自退。
    handles.shell_watch_task = asyncio.create_task(_watch_shell_pid())


def _task_lastwords_recover(handles: LifespanHandles) -> None:
    # 原块 36（无 try → abort）。遗言恢复（docs/lastwords-design.md §1）：
    # 上个进程留下的 lastwords/ 快照按 mode 注入回原 session（notify 折叠带
    # / auto headless 续跑）。后台 task + 全 try 包裹——恢复失败绝不炸启动；
    # 延时 2s 让 MCP/会话装配先行（注入要 _ensure_session 就绪）。
    handles.lastwords_recover_task = asyncio.create_task(_lastwords_recover_task())


#: 启动任务表。**表序即执行序**，与 server.py 原 lifespan 平铺块 1-36
#: 一一对应（见各任务体「原块 N」注释）；任何插入/删除/重排都必须同步
#: tests/unit/test_startup_tasks.py 的快照断言。
STARTUP_TASKS: tuple[PhaseTask, ...] = (
    PhaseTask(
        name="ensure_layout",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_ensure_layout,
    ),
    PhaseTask(
        name="startup_maintenance",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_startup_maintenance,
    ),
    PhaseTask(
        name="search_index_build",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_search_index_build,
    ),
    PhaseTask(
        name="startup_check",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_startup_check,
    ),
    PhaseTask(
        name="delegation_reconcile",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_delegation_reconcile,
    ),
    PhaseTask(
        name="plan_watcher_recovery",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_plan_watcher_recovery,
    ),
    PhaseTask(
        name="team_recovery",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_team_recovery,
    ),
    PhaseTask(
        name="i18n_health_check",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_i18n_health_check,
    ),
    PhaseTask(
        name="set_main_loop",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_set_main_loop,
    ),
    PhaseTask(
        name="anyio_thread_limiter",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_anyio_thread_limiter,
    ),
    PhaseTask(
        name="asyncio_default_executor",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_asyncio_default_executor,
    ),
    PhaseTask(
        name="requeue_pending_titles",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        depends_on=("set_main_loop",),
        run=_task_requeue_pending_titles,
    ),
    PhaseTask(
        name="apply_system_proxy",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_apply_system_proxy,
    ),
    PhaseTask(
        name="ensure_turn_log",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_ensure_turn_log,
    ),
    PhaseTask(
        name="usage_cleanup",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_usage_cleanup,
    ),
    PhaseTask(
        name="memory_sweep",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_memory_sweep,
    ),
    PhaseTask(
        name="session_files_migration",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_session_files_migration,
    ),
    PhaseTask(
        name="hooks_init",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_hooks_init,
    ),
    PhaseTask(
        name="mods_classic_hooks",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        depends_on=("hooks_init",),
        run=_task_mods_classic_hooks,
    ),
    PhaseTask(
        name="todo_seed",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_todo_seed,
    ),
    PhaseTask(
        name="connector_seed",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_connector_seed,
    ),
    PhaseTask(
        name="browser_connector_setup",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_browser_connector_setup,
    ),
    PhaseTask(
        name="agents_registry_seed",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_agents_registry_seed,
    ),
    PhaseTask(
        name="web_permissions_migration",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_web_permissions_migration,
    ),
    PhaseTask(
        name="workflows_seed",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_workflows_seed,
    ),
    PhaseTask(
        name="subagent_type_seed",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_subagent_type_seed,
    ),
    PhaseTask(
        name="subagent_reconcile",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_subagent_reconcile,
    ),
    PhaseTask(
        name="run_reconciliation",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_run_reconciliation,
    ),
    PhaseTask(
        name="refresh_session_metas",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_refresh_session_metas,
    ),
    PhaseTask(
        name="mcp_registry_init",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_mcp_registry_init,
    ),
    PhaseTask(
        name="mcp_background_connect",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        depends_on=("mcp_registry_init",),
        run=_task_mcp_background_connect,
    ),
    PhaseTask(
        name="scheduler_start",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_scheduler_start,
    ),
    PhaseTask(
        name="tasks_registry_start",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_CONTINUE,
        run=_task_tasks_registry_start,
    ),
    PhaseTask(
        name="devswap_watch",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_devswap_watch,
    ),
    PhaseTask(
        name="shell_watch",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_shell_watch,
    ),
    PhaseTask(
        name="lastwords_recover",
        phase=PHASE_STARTUP,
        fail_policy=FAIL_ABORT,
        run=_task_lastwords_recover,
    ),
)


# --------------------------------------------------------------------------- #
# shutdown 任务体（迁自 server.py lifespan 的 finally 段；「原块 S-N」=
# 原平铺顺序，语义零改动）
# --------------------------------------------------------------------------- #

def _task_lastwords_mark_shutdown(handles: LifespanHandles) -> None:
    # 原块 S-1。遗言保护（lastwords.py SIGTERM 诊断 1a）：置位关闭旗——
    # 此后 asyncio loop teardown 取消残余 turn 协程触发的 note_turn_end
    # 不得删掉 write_lastwords(signal) 刚落盘的遗言。
    from . import lastwords as _lastwords

    _lastwords.mark_shutdown()


def _task_cancel_background_tasks(handles: LifespanHandles) -> None:
    # 原块 S-2（无 try → abort，与原 finally 行为一致）。
    if not handles.mcp_connect_task.done():
        handles.mcp_connect_task.cancel()
    handles.devswap_watch_task.cancel()
    handles.shell_watch_task.cancel()
    handles.lastwords_recover_task.cancel()


def _task_lastwords_write_signal(handles: LifespanHandles) -> None:
    # 原块 S-3。遗言落盘（docs/lastwords-design.md §1 SIGTERM 兜底）：
    # prepare API 漏调（人肉 kill / 壳直接重启）时这里是最后一道。同步
    # 快速 IO——遍历活跃 turn 读 checkpoint + 落盘，预算 <2s（uvicorn 优雅
    # 关闭窗口 timeout_graceful_shutdown=3s）；复杂逻辑一律放 prepare API。
    from . import lastwords as _lastwords

    _lastwords.write_lastwords(_lastwords.REASON_SIGNAL)


async def _task_scheduler_stop(handles: LifespanHandles) -> None:
    # 原块 S-4（内层 try 有独立日志 tag，原样保留）。取消调度循环 + 在途
    # 执行协程（契约同 Goal driver / _connect_mcp_background）。
    if handles.scheduler_task is not None:
        try:
            from . import scheduler as _scheduler

            await _scheduler.stop()
        except Exception:
            _log.exception("scheduler_shutdown_failed")


async def _task_run_tasks_shutdown(handles: LifespanHandles) -> None:
    # 原块 S-5。Cancel live workflow-run tasks and mark any still-running
    # run interrupted so a clean quit never strands a "running" run. A hard
    # kill -9 skips this; startup reconciliation is the backstop.
    from .api.workflows import _shutdown_run_tasks

    await _shutdown_run_tasks()


async def _task_synth_tasks_shutdown(handles: LifespanHandles) -> None:
    # 原块 S-6。Cancel live synthesis tasks (their cases stay without
    # output.json and render 未完成 after the restart — intentional, no
    # reconciliation).
    from .api.workflows import _shutdown_synth_tasks

    await _shutdown_synth_tasks()


async def _task_tasks_registry_stop(handles: LifespanHandles) -> None:
    # 原块 S-7（内层 try 有独立日志 tag，原样保留）。后台任务收尾（D4）：
    # 子进程随 sidecar 退出——SIGTERM 全体、宽限后 SIGKILL，重启对账负责
    # 把它们标成 interrupted。
    if handles.tasks_registry_started:
        try:
            from .tasks import get_registry as _get_tasks_registry

            await _get_tasks_registry().stop_all()
        except Exception:
            _log.exception("tasks registry shutdown failed")


async def _task_browser_profile_shutdown(handles: LifespanHandles) -> None:
    # 原块 S-8。B-track profile Chrome 随 sidecar 退（2026-11-03 进程泄露
    # 盘点）：此前 shutdown() 只挂在连接器页的「停止」按钮上，sidecar 退出
    # （无论优雅与否）都不收——桌面退出改 SIGTERM-first 后这条路径真正
    # 可达了。单例构造不 spawn；从未启动过时 conn/proc 均为 None，安全
    # no-op。
    from .browser.executor import get_profile_backend as _gpb

    await _gpb().shutdown()


async def _task_mcp_close_all(handles: LifespanHandles) -> None:
    # 原块 S-9（无 try → abort，与原 finally 行为一致）。
    if shared._mcp:
        await shared._mcp.close_all()


async def _task_mods_channel_shutdown(handles: LifespanHandles) -> None:
    # 原块 S-10。Mods bus teardown (§5.2): stop the channel and any
    # dev-spawned broker child. Best-effort; never masks the shutdowns
    # above.
    from .mods.channel import shutdown_channel

    await shutdown_channel()


#: 关闭任务表。**表序即执行序**，与 server.py 原 lifespan finally 段平铺
#: 块 S-1..S-10 一一对应（见各任务体「原块 S-N」注释）。
SHUTDOWN_TASKS: tuple[PhaseTask, ...] = (
    PhaseTask(
        name="lastwords_mark_shutdown",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_lastwords_mark_shutdown,
    ),
    PhaseTask(
        name="cancel_background_tasks",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_ABORT,
        run=_task_cancel_background_tasks,
    ),
    PhaseTask(
        name="lastwords_write_signal",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_lastwords_write_signal,
    ),
    PhaseTask(
        name="scheduler_stop",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        depends_on=("cancel_background_tasks",),
        run=_task_scheduler_stop,
    ),
    PhaseTask(
        name="run_tasks_shutdown",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_run_tasks_shutdown,
    ),
    PhaseTask(
        name="synth_tasks_shutdown",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_synth_tasks_shutdown,
    ),
    PhaseTask(
        name="tasks_registry_stop",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        depends_on=("cancel_background_tasks",),
        run=_task_tasks_registry_stop,
    ),
    PhaseTask(
        name="browser_profile_shutdown",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_browser_profile_shutdown,
    ),
    PhaseTask(
        name="mcp_close_all",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_ABORT,
        run=_task_mcp_close_all,
    ),
    PhaseTask(
        name="mods_channel_shutdown",
        phase=PHASE_SHUTDOWN,
        fail_policy=FAIL_CONTINUE,
        run=_task_mods_channel_shutdown,
    ),
)
