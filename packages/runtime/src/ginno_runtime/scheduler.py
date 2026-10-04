"""定时任务调度器（scheduled-tasks-design.md §3.7）。

单例 asyncio 调度循环：30s tick，随 lifespan 启动/取消（与 Goal driver /
``_connect_mcp_background`` 同契约）。职责：

* next_run 计算：四种计划（interval/daily/weekly/once），本地时区、DST 安全
  （本地日历语义：每日 09:30 在 DST 切换日仍是本地 09:30）；``next_run_at``
  持久化回 schedules.json。
* 触发：``now >= next_run_at`` 且延迟 ≤ GRACE(60s) 则触发；超过 GRACE 的每个
  错过计划点记一条 missed（只记账不补跑，§10 决议 1）后跳到未来。
* 双目标分流：prompt → 影子会话 + headless ``_run_stream``（协议抄
  ``_run_goal_turn``）；workflow → ``wf_store.create_run(origin="schedule")`` +
  ``_spawn_run_task(_run_workflow_bg(...))``（即 POST /api/workflow_runs 的
  headless 内联版），5s 步进轮询 run 终态回写执行记录。
* 同任务不重叠：上次仍在跑 → 记 skipped_overlap；任务间允许并行。
* LLM 摘要（§10 决议 6）：执行成功后一次 ≤160 字廉价调用，fire-and-forget
  不阻塞调度；失败回退纯截断；计入 usage（source="schedule"）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from . import models as models_mod
from . import providers as prov_mod
from . import schedule_store as store
from . import server_shared as shared
from . import usage as usage_mod
from . import usage_store
from . import workflows as wf_store
from .i18n import _
from .schedule_store import MIN_INTERVAL_MIN

_log = logging.getLogger("ginno.schedule")

TICK_S = 30.0
GRACE_S = 60.0  # 延迟 ≤60s 仍触发；超过即记 missed（§3.7）
POLL_RUN_S = 5.0  # workflow run 终态轮询步进
SUMMARY_MAX_CHARS = 160

# 同任务不重叠：task_id -> run_id（进行中的执行）。进程内真值；跨重启的孤儿
# 由 reconcile_interrupted 对账。
_RUNNING: dict[str, str] = {}
# 进行中的执行协程（run_id -> Task），强引用防 GC + 测试可 await。
_RUN_TASKS: dict[str, asyncio.Task] = {}

# 单例循环任务（lifespan 注册/停机）。
_LOOP_TASK: asyncio.Task | None = None


# --------------------------------------------------------------------------- #
# 事件总线（WS /api/ws/schedule 的推送半边；模式同 connectors/events.py）
# --------------------------------------------------------------------------- #
Listener = Callable[[str, dict], None]


class ScheduleEvents:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Listener) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def emit(self, type_: str, data: dict | None = None) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(type_, data or {})
            except Exception:  # noqa: BLE001 — 坏监听者不拖垮调度
                pass


_events: ScheduleEvents | None = None


def schedule_events() -> ScheduleEvents:
    global _events
    if _events is None:
        _events = ScheduleEvents()
    return _events


# --------------------------------------------------------------------------- #
# next_run 计算（本地日历语义，DST 安全）
# --------------------------------------------------------------------------- #
def _local_tz():
    return datetime.now().astimezone().tzinfo


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def _wall_ts(dt: datetime, tz) -> float:
    """本地墙上时间（naive）→ epoch。``tz`` 显式挂回再换算，DST 缺失时间
    （春季前拨的 02:xx）由 PEP 495 fold=0 语义顺延到存在的本地时间。"""
    return dt.replace(tzinfo=tz).timestamp()


def next_run_ts(schedule: dict, now: float | None = None, tz=None) -> float | None:
    """一种计划的下一个触发点（epoch 秒）；无未来点返回 None（once 已过）。

    ``now``/``tz`` 可注入供测试：``tz`` 传 ``zoneinfo.ZoneInfo(...)`` 即可模拟
    DST 切换（默认取系统本地时区）。daily/weekly/once 走本地日历语义——
    「每天 09:30」永远落在当地日历的 09:30，而不是固定 24h 间隔。
    """
    sch = schedule or {}
    kind = sch.get("kind")
    tz = tz or _local_tz()
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz).replace(tzinfo=None)
    if kind == "interval":
        # interval 走 epoch 网格（无日历语义）；带 from_point 的锚定版见 compute_next
        minutes = max(MIN_INTERVAL_MIN, int(sch.get("minutes") or MIN_INTERVAL_MIN))
        base = time.time() if now is None else float(now)
        return base + minutes * 60
    if kind == "daily":
        h, m = _parse_hhmm(sch.get("at") or "00:00")
        cand = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now_dt:
            cand += timedelta(days=1)
        return _wall_ts(cand, tz)
    if kind == "weekly":
        h, m = _parse_hhmm(sch.get("at") or "00:00")
        cand = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        # 契约：weekday 0=周日（§4.1）；Python 日历 Monday=0 … Sunday=6，换算之
        target_wd = (int(sch.get("weekday") or 0) + 6) % 7
        # 先对齐到目标时刻，再前进到目标星期；<= 判断保证「严格未来」
        while cand <= now_dt or cand.weekday() != target_wd:
            cand += timedelta(days=1)
        return _wall_ts(cand, tz)
    if kind == "once":
        try:
            cand = datetime.fromisoformat(str(sch.get("at") or ""))
        except ValueError:
            return None
        if cand <= now_dt:
            return None
        return _wall_ts(cand, tz)
    return None


def _next_interval_point(sch: dict, now: float, from_point: float | None) -> float:
    """interval 计划的下一个点：锚定在上一计划点（保持网格）。不做 now 之上的
    跨步——追赶语义由 :func:`_skip_missed` 逐点记账。"""
    minutes = max(MIN_INTERVAL_MIN, int(sch.get("minutes") or MIN_INTERVAL_MIN))
    return (from_point if from_point else now) + minutes * 60


def compute_next(task: dict, now: float, *, from_point: float | None = None) -> float | None:
    """重算任务的 next_run_at。interval 从 ``from_point``（上次计划点）锚定
    网格；其余类型从本地日历取「严格未来」的点。"""
    sch = task.get("schedule") or {}
    if sch.get("kind") == "interval":
        return _next_interval_point(sch, now, from_point)
    return next_run_ts(sch, now=now)


def ensure_next_run(task: dict) -> float | None:
    """任务缺 next_run_at（新建/老数据）时补算并持久化。"""
    if task.get("next_run_at"):
        return task["next_run_at"]
    nxt = compute_next(task, time.time())
    if nxt is not None:
        store.patch_task(task["id"], {"next_run_at": nxt})
    return nxt


# --------------------------------------------------------------------------- #
# 启动对账（§3.7）：当日 jsonl 里残留 running 的孤儿行改记 error("interrupted")
# --------------------------------------------------------------------------- #
def reconcile_interrupted() -> int:
    """孤儿 running 行（上次崩溃/被杀）→ error("interrupted")（§3.7）。

    扫今天与前一天两个日文件：跨午夜存活（昨天启动、今天被杀）的 running 行
    落在前一天的文件里。按去重后的末行判断，天然不碰已有终态的 run。"""
    now = time.time()
    healed = 0
    seen: set[str] = set()
    for day in (store._date_str(now - 86400), store._today()):
        for e in store.dedupe_runs(store.load_day(day)):
            if e.get("status") != "running":
                continue
            rid = e.get("run_id") or ""
            if rid in seen:
                continue
            seen.add(rid)
            e = {**e, "status": "error", "error": "interrupted", "finished_at": now}
            store.record_run(e)
            healed += 1
    if healed:
        _log.info("schedule_run_reconciliation interrupted=%d", healed)
    return healed


# --------------------------------------------------------------------------- #
# 触发执行
# --------------------------------------------------------------------------- #
def _run_row(task: dict, run_id: str, trigger: str, scheduled_at: float, started: float) -> dict:
    target = task.get("target") or {}
    return {
        "run_id": run_id,
        "task_id": task.get("id"),
        "task_name": task.get("name") or "",  # 任务名冗余，任务删除后仍可读（§4.2）
        "target_type": target.get("type"),
        "trigger": trigger,
        "status": "running",
        "scheduled_at": scheduled_at,
        "started_at": started,
        "finished_at": None,
        "session_id": None,
        "workflow_id": target.get("workflow_id"),
        "workflow_run_id": None,
        "summary": None,
        "error": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def trigger_task(
    task: dict, *, trigger: str = "schedule", scheduled_at: float | None = None
) -> str:
    """触发一次执行（调度到点与「立即执行」共用入口）。同任务进行中 → 记
    skipped_overlap 不排队（§3.7）。返回本次 run_id。"""
    tid = task.get("id") or ""
    now = time.time()
    scheduled_at = float(scheduled_at if scheduled_at is not None else now)
    if tid in _RUNNING:
        row = _run_row(task, store.new_run_id(), trigger, scheduled_at, now)
        row["status"] = "skipped_overlap"
        row["finished_at"] = now
        store.record_run(row)
        schedule_events().emit("run_finished", {"run": row})
        return row["run_id"]

    run_id = store.new_run_id()
    row = _run_row(task, run_id, trigger, scheduled_at, now)
    store.record_run(row)
    _RUNNING[tid] = run_id
    schedule_events().emit("run_started", {"run": row})
    # 任务级 last_run 冗余展示字段 + next_run_at 推进（fire-and-forget 之前落盘）
    store.patch_task(tid, {"last_run": {"run_id": run_id, "status": "running", "started": now}})
    if trigger == "schedule":
        _advance_next(task, scheduled_at)
    schedule_events().emit("task_updated", {"task": store.get_task(tid) or task})
    t = asyncio.create_task(_execute_run(task, row))
    _RUN_TASKS[run_id] = t

    def _done(_t: asyncio.Task) -> None:
        _RUN_TASKS.pop(run_id, None)
        # 兜底：任务在首步执行前即被取消时协程体（含 _execute_run 的 finally）
        # 不会运行，重叠槽在此确保释放
        if _RUNNING.get(tid) == run_id:
            _RUNNING.pop(tid, None)

    t.add_done_callback(_done)
    return run_id


def _step_point(task: dict, now: float, point: float) -> float | None:
    """missed 链的一步。interval 沿网格 +step（可能仍 ≤ now，由链循环逐点记账）；
    日历类从 ``point`` 取其后严格未来的下一个日历点——多日停机时每个错过的
    计划点各记一条 missed（§3.7），而不是跳到 now 之后的唯一点只记一条。"""
    sch = task.get("schedule") or {}
    if sch.get("kind") == "interval":
        minutes = max(MIN_INTERVAL_MIN, int(sch.get("minutes") or MIN_INTERVAL_MIN))
        return point + minutes * 60
    return compute_next(task, point + 1e-6)


def _skip_missed(task: dict, point: float) -> float | None:
    """从 ``point`` 起把 GRACE 之外的每个错过计划点记一条 missed（§10 决议 1），
    返回第一个未错过的计划点（interval 可能在 GRACE 窗口内或未来；once/日历类
    无未来点时为 None）。"""
    now = time.time()
    future: float | None = point
    while future is not None and now - future > GRACE_S:
        _record_missed(task, future)
        future = _step_point(task, now, future)
    return future


def _advance_next(task: dict, from_point: float) -> None:
    """调度触发后推进 next_run_at 并持久化（manual 触发不动计划）。执行期间
    错过的中间点同样逐条补记 missed（与 _tick 对账同语义）。"""
    future = _skip_missed(task, from_point)
    if sch_kind(task) == "once" and future is None:
        # 单次任务到点执行完自动暂停（§3.2）
        store.patch_task(task["id"], {"next_run_at": None, "enabled": False})
    else:
        store.patch_task(task["id"], {"next_run_at": future})


def sch_kind(task: dict) -> str:
    return (task.get("schedule") or {}).get("kind") or ""


async def _execute_run(task: dict, row: dict) -> None:
    """单次执行的完整生命周期：分流 → 终态回写 → 摘要（fire-and-forget）。

    重叠槽 ``_RUNNING`` 在 finally 里无条件释放（含取消路径），否则停机取消后
    该任务后续触发会被永久记 skipped_overlap。"""
    run_id = row["run_id"]
    tid = task.get("id") or ""
    try:
        try:
            ttype = (task.get("target") or {}).get("type")
            if ttype == "workflow":
                final = await _run_workflow_target(task, row)
            else:
                final = await _run_prompt_target(task, row)
        except asyncio.CancelledError:
            # 停机取消在途执行：落 interrupted 终态行（与启动对账同语义），
            # 不走摘要。startup reconcile 以「running 存在」为准，不会重复记账。
            now = time.time()
            row2 = {
                **row, "status": "error", "error": "interrupted", "finished_at": now,
                # User-facing row copy rendered server-side (no request context
                # here — `_()` resolves the settings locale, i18n-design.md §5).
                "summary": _("schedule.run_interrupted"),
            }
            store.record_run(row2)
            schedule_events().emit("run_finished", {"run": row2})
            raise
        except Exception as exc:  # noqa: BLE001 — 执行器崩溃也要落终态行
            _log.exception("schedule_run_failed run=%s task=%s", run_id, task.get("id"))
            final = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "text": ""}

        now = time.time()
        ok = bool(final.get("ok"))
        text = str(final.get("text") or "")
        status_text = (
            _("schedule.run_succeeded")
            if ok
            else _("schedule.run_failed", error=final.get("error") or "unknown error")
        )
        row2 = {
            **row,
            "status": "ok" if ok else "error",
            "error": None if ok else (final.get("error") or "failed"),
            "finished_at": now,
            # 摘要永远不缺席：先落纯截断兜底，LLM 摘要成功后以同 run_id 末行替位（§10 决议 6）
            "summary": (text[:SUMMARY_MAX_CHARS] if text else status_text)[:SUMMARY_MAX_CHARS],
            "input_tokens": final.get("input_tokens"),
            "output_tokens": final.get("output_tokens"),
        }
        store.record_run(row2)
        store.patch_task(task["id"], {
            "last_run": {
                "run_id": run_id, "status": row2["status"],
                "started": row["started_at"], "finished": now,
            },
        })
        schedule_events().emit("run_finished", {"run": row2})
        schedule_events().emit("task_updated", {"task": store.get_task(task["id"])})

        if ok and text:
            # LLM 摘要：fire-and-forget，不阻塞 next_run_at 推进（§3.7）
            shared.spawn_bg(_llm_summary(task, row2, text))
    finally:
        _RUNNING.pop(tid, None)


async def _run_prompt_target(task: dict, row: dict) -> dict:
    """prompt 目标：新建影子会话 + headless 跑一轮 _run_stream（协议抄 _run_goal_turn）。"""
    from .api import sessions as _sessions_api
    from .api import stream as _stream_api
    from .api.sessions import _turn_last_error
    from .tools.ask_tools import reset_interactive as _reset_interactive_turn
    from .tools.ask_tools import set_interactive as _set_interactive_turn

    target = task.get("target") or {}
    slug = target.get("project_slug") or "default"
    req = _sessions_api.CreateSessionRequest(
        project_slug=slug,
        workspace="",  # create_session 以每会话目录为准（忽略该字段）
        agent_id=target.get("agent_id"),
        title=f"⏰ {task.get('name') or _('schedule.default_task_title')}",
        type="scheduled",
    )
    meta = await _sessions_api.create_session(req)
    if meta.get("ok") is False or not meta.get("id"):
        return {"ok": False, "error": str(meta.get("error") or "会话创建失败"), "text": ""}
    session_id = meta["id"]
    row["session_id"] = session_id
    # 运行中即可回放：把 session_id 回填进 running 行（jsonl 末行胜出），并广播
    # 让前端刷新拿到可点行。
    store.record_run(row)
    schedule_events().emit("run_started", {"run": row})
    # meta 标 schedule_run_id（§4.3）；type 已由 CreateSessionRequest.type 落盘
    from .session_meta import _session_meta_patch

    _session_meta_patch(slug, session_id, {"schedule_run_id": row["run_id"]})

    session = shared._SESSIONS.get(session_id)
    if not session:
        return {"ok": False, "error": "会话未就绪", "text": ""}
    agent_id = session.get("agent_id")
    turn_id = uuid.uuid4().hex  # headless 一轮 = 一 turn
    text = str(target.get("prompt") or "")
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            # usage 打标 source="schedule"（§4.4；manual 与摘要调用同 source）
            "usage_source": "schedule",
        }
    }
    _log.info(
        "schedule_run_start run=%s task=%s session=%s type=prompt",
        row["run_id"], task.get("id"), session_id,
    )
    # 早置 running 标志 + headless ask_user 关闭——与 _run_goal_turn 同协议
    shared._RUNNING_TURNS[session_id] = turn_id
    ask_tok = _set_interactive_turn(False)
    try:
        await _stream_api._run_stream(None, session["graph"], config, text, session, agent_id)
    finally:
        _reset_interactive_turn(ask_tok)
        from .server_shared import _PENDING_RESUME, _RUNNING_TURNS

        if _RUNNING_TURNS.get(session_id) == turn_id and session_id not in _PENDING_RESUME:
            _RUNNING_TURNS.pop(session_id, None)

    err = _turn_last_error(session_id, turn_id)
    if err:
        return {"ok": False, "error": err, "text": ""}
    inp, out = _usage_tokens(session_id=session_id, since=row["started_at"])
    last = await _last_assistant_text(session_id, slug)
    return {"ok": True, "error": None, "text": last, "input_tokens": inp, "output_tokens": out}


async def _run_workflow_target(task: dict, row: dict) -> dict:
    """workflow 目标：headless run（POST /api/workflow_runs 的内联版 + origin 标记）。"""
    from .api.workflows import _run_workflow_bg, _spawn_run_task

    target = task.get("target") or {}
    workflow_id = str(target.get("workflow_id") or "")
    wf = wf_store.get_def(workflow_id)
    if not wf:
        return {"ok": False, "error": "workflow_missing", "text": ""}
    dsl = wf.get("dsl") or {}
    ctx = dsl.get("context") or {}
    missing = store.missing_required_inputs(
        ctx.get("schema"), ctx.get("initial"), target.get("context_override")
    )
    if missing:
        return {"ok": False, "error": f"missing_input: {', '.join(missing)}", "text": ""}
    override = target.get("context_override")
    if not isinstance(override, dict):
        override = None
    run = wf_store.create_run(
        wf,
        session_id=None,
        present_in_session_id=None,
        context_override=override,
        origin="schedule",
    )
    row["workflow_run_id"] = run["id"]
    _log.info(
        "schedule_run_start run=%s task=%s wf_run=%s type=workflow",
        row["run_id"], task.get("id"), run["id"],
    )
    _spawn_run_task(run["id"], _run_workflow_bg(run["id"], workflow_id, override, None))
    # 5s 步进轮询 run 终态（§3.7）
    while True:
        cur = wf_store.get_run(run["id"])
        if not cur or cur.get("status") in wf_store.TERMINAL_STATUSES:
            break
        await asyncio.sleep(POLL_RUN_S)
    status = (cur or {}).get("status")
    if status == "done":
        inp, out = _usage_tokens(run_id=run["id"], since=row["started_at"])
        text = await _run_final_output(run["id"], cur or {})
        return {"ok": True, "error": None, "input_tokens": inp, "output_tokens": out, "text": text}
    return {"ok": False, "error": (cur or {}).get("error") or f"run {status}", "text": ""}


# --------------------------------------------------------------------------- #
# 结果收集 + LLM 摘要
# --------------------------------------------------------------------------- #
def _usage_tokens(
    *, session_id: str | None = None, run_id: str | None = None, since: float
) -> tuple[int, int]:
    """从 usage 日志聚合本次执行的 tokens（prompt 目标按 session、workflow 按
    run turn_id 关联）。usage 记录是账本真值，这里只读不写。"""
    want = session_id or run_id or ""
    key = "session_id" if session_id else "turn_id"
    frm = time.strftime("%Y-%m-%d", time.localtime(since))
    inp = out = 0
    try:
        today = time.strftime("%Y-%m-%d", time.localtime())
        for e in usage_store.iter_range(frm, today):
            if e.get(key) == want:
                inp += int(e.get("input_tokens") or 0)
                out += int(e.get("output_tokens") or 0)
    except Exception:  # noqa: BLE001
        pass
    return inp, out


async def _last_assistant_text(session_id: str, slug: str) -> str:
    """prompt 目标摘要输入 = 末条助手消息（§3.7）。"""
    try:
        from .checkpointer import FileCheckpointer

        tup = await FileCheckpointer(slug).aget_tuple({"configurable": {"thread_id": session_id}})
        messages = (
            (tup.checkpoint.get("channel_values") or {}).get("messages")
            if tup and tup.checkpoint else []
        )
        for m in reversed(messages or []):
            if getattr(m, "type", None) == "ai":
                from .graph import text_of_content

                t = text_of_content(m.content).strip()
                if t:
                    return t
    except Exception:  # noqa: BLE001
        pass
    return ""


async def _run_final_output(run_id: str, run: dict) -> str:
    """workflow 目标摘要输入 = run 最终输出（§3.7）：优先 run 记录的步产出；
    agent 步的 result 文本只落在 state ``results`` 通道，则从 run 检查点兜底读取。"""
    for s in reversed(run.get("steps") or []):
        t = str(s.get("output") or "").strip()
        if t:
            return t
    try:
        from .checkpointer import FileCheckpointer

        tup = await FileCheckpointer("default").aget_tuple({"configurable": {"thread_id": run_id}})
        results = (
            (tup.checkpoint.get("channel_values") or {}).get("results")
            if tup and tup.checkpoint else None
        )
        if isinstance(results, dict) and results:
            text = str(list(results.values())[-1] or "").strip()
            # WRITE_JSON 尾巴是机器协议，对摘要无意义
            cut = text.find("\nWRITE_JSON")
            if cut != -1:
                text = text[:cut]
            return text.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


_SUMMARY_SYSTEM = (
    "你是执行摘要助手。把给定的执行结果压缩成不超过 160 字的中文摘要，"
    "只输出摘要文本本身，不要任何前缀、引号或解释。"
)


async def _llm_summary(task: dict, row: dict, text: str) -> None:
    """一次 ≤160 字的廉价摘要调用（任务同款模型）。成功 → 同 run_id 追加末行
    替位摘要；失败 → 纯截断兜底已在终态行里，摘要永远不缺席（§10 决议 6）。
    调用计入 usage（source="schedule"）。"""
    provider = None
    model_name = None
    session_id = row.get("session_id")
    if session_id:
        from .session_meta import _find_meta

        meta, _ = _find_meta(session_id) or ({}, None)
        provider = (meta or {}).get("provider")
        model_name = (meta or {}).get("model")
    provider = provider or prov_mod.get_default_provider()
    model_name = model_name or prov_mod.model_for_provider(prov_mod.load_providers(), provider)
    try:
        model = models_mod.build_model(provider, model_name)
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await model.ainvoke(
            [SystemMessage(content=_SUMMARY_SYSTEM), HumanMessage(content=text[:4000])]
        )
        from .graph import text_of_content

        s = text_of_content(resp.content).strip()[:SUMMARY_MAX_CHARS]
        u = usage_mod.extract_usage(resp)
        if u:
            usage_store.record(
                input_tokens=u["input_tokens"],
                output_tokens=u["output_tokens"],
                cache_read_tokens=u["cache_read_tokens"],
                cache_creation_tokens=u["cache_creation_tokens"],
                provider=provider or "",
                model=model_name or "",
                source="schedule",
                session_id=session_id,
                project_slug=(task.get("target") or {}).get("project_slug"),
                agent_id=(task.get("target") or {}).get("agent_id"),
                turn_id=row.get("run_id"),
            )
    except Exception:  # noqa: BLE001 — 断网/模型失败：回退纯截断（终态行已有）
        _log.warning("schedule_summary_failed run=%s", row.get("run_id"), exc_info=True)
        return
    if not s:
        return
    # 同 run_id 追加末行（末行胜出），只更新 summary 字段
    updated = {**row, "summary": s}
    store.record_run(updated)
    schedule_events().emit("run_finished", {"run": updated})


# --------------------------------------------------------------------------- #
# 调度循环
# --------------------------------------------------------------------------- #
def _record_missed(task: dict, point: float) -> None:
    row = _run_row(task, store.new_run_id(), "schedule", point, point)
    row["status"] = "missed"
    row["finished_at"] = point
    row["summary"] = _("schedule.missed")
    store.record_run(row)
    schedule_events().emit("missed", {"run": row, "task_id": task.get("id")})


async def _tick() -> None:
    cfg = store.load_config()
    if not cfg.get("enabled", True):
        return
    now = time.time()
    for task in list(cfg.get("tasks") or []):
        if not task.get("enabled"):
            continue
        tid = task.get("id")
        nxt = task.get("next_run_at")
        if not nxt:
            nxt = ensure_next_run(task)
            if nxt is None:
                continue
        nxt = float(nxt)
        delta = now - nxt
        if delta < 0:
            continue  # 还没到点
        if delta > GRACE_S:
            # 错过链：每个错过的计划点记一条 missed，跳到第一个未错过点（§10 决议 1）
            future = _skip_missed(task, nxt)
            patch: dict[str, Any] = {"next_run_at": future}
            if sch_kind(task) == "once":
                patch["enabled"] = False  # once 错过即完成生命周期
            store.patch_task(tid, patch)
            schedule_events().emit("task_updated", {"task": store.get_task(tid)})
            continue
        # 到点（延迟 ≤ GRACE）：触发；调度器协程内串行触发即可，执行本身并行
        await asyncio.sleep(0)
        trigger_task(task, trigger="schedule", scheduled_at=nxt)


async def scheduler_loop() -> None:
    """单例调度循环。随 lifespan cancel；CancelledError = 正常停机。"""
    _log.info("scheduler_loop_started tick=%ss grace=%ss", TICK_S, GRACE_S)
    try:
        while True:
            try:
                await _tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 单 tick 崩溃不终止循环
                _log.exception("scheduler_tick_failed")
            await asyncio.sleep(TICK_S)
    except asyncio.CancelledError:
        pass
    finally:
        global _LOOP_TASK
        if _LOOP_TASK is asyncio.current_task():
            _LOOP_TASK = None
        _log.info("scheduler_loop_stopped")


def start() -> asyncio.Task:
    """lifespan 启动入口（幂等）。"""
    global _LOOP_TASK
    if _LOOP_TASK is not None and not _LOOP_TASK.done():
        return _LOOP_TASK
    try:
        reconcile_interrupted()
    except Exception:  # noqa: BLE001
        _log.exception("schedule_reconciliation_failed")
    _LOOP_TASK = asyncio.get_running_loop().create_task(scheduler_loop())
    return _LOOP_TASK


async def stop() -> None:
    """lifespan 停机入口：cancel 循环 + 等待在途执行协程收尾。"""
    global _LOOP_TASK
    if _LOOP_TASK is not None and not _LOOP_TASK.done():
        _LOOP_TASK.cancel()
        try:
            await asyncio.wait_for(_LOOP_TASK, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
    _LOOP_TASK = None
    pending = [t for t in list(_RUN_TASKS.values()) if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=3.0)
        except (TimeoutError, asyncio.CancelledError):
            pass


# --------------------------------------------------------------------------- #
# 时间条：当天剩余计划点（§5.1 timeline）
# --------------------------------------------------------------------------- #
def planned_points(task: dict, day: str, now: float | None = None) -> list[float]:
    """某任务在 ``day``（YYYY-MM-DD）内、now 之后的计划点（epoch 秒）。
    interval 从 next_run_at 起按网格步进；其余类型算该日历日的唯一点。"""
    now = time.time() if now is None else now
    sch = task.get("schedule") or {}
    kind = sch.get("kind")
    tz = _local_tz()
    day_dt = datetime.strptime(day, "%Y-%m-%d")
    out: list[float] = []
    if kind == "interval":
        step = max(MIN_INTERVAL_MIN, int(sch.get("minutes") or MIN_INTERVAL_MIN)) * 60
        pt = task.get("next_run_at") or (now if task.get("enabled") else None)
        if pt is None:
            return []
        pt = float(pt)
        eod = _wall_ts(day_dt + timedelta(days=1), tz)
        while pt < now:
            pt += step
        while pt < eod:
            out.append(pt)
            pt += step
        return out
    if kind == "once":
        try:
            cand = datetime.fromisoformat(str(sch.get("at") or ""))
        except ValueError:
            return []
        if cand.strftime("%Y-%m-%d") != day:
            return []
        ts = _wall_ts(cand, tz)
        return [ts] if ts >= now else []
    if kind in ("daily", "weekly"):
        try:
            h, m = _parse_hhmm(sch.get("at") or "00:00")
        except ValueError:
            return []
        cand = day_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if kind == "weekly" and cand.weekday() != (int(sch.get("weekday") or 0) + 6) % 7:
            return []
        ts = _wall_ts(cand, tz)
        return [ts] if ts >= now else []
    return []
