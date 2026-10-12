"""Plan watcher — 清单编排循环（subagent-plan-orchestration-design.md §3/§4）.

per-parent 后台 asyncio 任务（同 goal driver 模式），只做三件事：

1. **调度**：open ∧ active 清单里 ready（pending ∧ 依赖全 done）的条目，
   受全局并发帽约束（create_subagent 自身会回 limit——watcher 收到即本
   tick 停手，下轮再试）。
2. **对账回写**：轮询式（10s tick，事件可提前唤醒）——running 条目对照
   子会话实况（subagent 看 subagent.status，delegation 看 stop_reason）
   回写终态。不做事件订阅：漏事件可自愈、幂等、一条代码路径。
3. **去抖合流唤醒**（D4）：检测到的条目终态进 per-session 缓冲，8s 静默
   或 30s 硬上限冲刷成一条注入（复用 _inject_result 的 steer/唤醒通道，
   child key = plan-<id>，前端暂按结果卡渲染，P3 换专用清单卡）。

watcher 不做业务决策（D3）：失败不自动重试，靠注入报告 + 主 agent amend。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..server_shared import _log, _push_session_event, spawn_bg
from . import store

_TICK_S = 10.0
_FLUSH_QUIET_S = 8.0
_FLUSH_MAX_S = 30.0

# sid -> asyncio.Task。会话没有 open 清单即退出；ensure_watcher 幂等。
_WATCHERS: dict[str, Any] = {}
_WAKE: dict[str, asyncio.Event] = {}

# sid -> {"first": monotonic, "notes": [str]}：去抖缓冲（进程内，重启即弃
# ——重启后首次 tick 不注入，等新事件）。
_PENDING_FLUSH: dict[str, dict[str, Any]] = {}


def ensure_watcher(session_id: str) -> None:
    """有 open 清单的会话起/保活 watcher（幂等；无事件循环时静默跳过——
    单测/CLI 场景）。"""
    if not session_id or session_id in _WATCHERS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _WAKE[session_id] = asyncio.Event()
    _WATCHERS[session_id] = loop.create_task(_watch_loop(session_id))
    _log.info("plan_watcher_start session=%s", session_id)


def notify_change(session_id: str) -> None:
    """清单被建/改/确认/取消时踢一脚（调度立即重算，不用等 tick）。"""
    ensure_watcher(session_id)
    evt = _WAKE.get(session_id)
    if evt is not None:
        evt.set()


def _session_slug(session_id: str) -> str:
    from ..session_meta import _find_meta

    meta, _ = _find_meta(session_id) or ({}, None)
    return (meta or {}).get("project_slug") or "default"


async def _watch_loop(session_id: str) -> None:
    try:
        while True:
            slug = _session_slug(session_id)
            plans = [
                p
                for p in store.list_plans(slug, session_id)
                if p.get("status") in store.PLAN_OPEN
            ]
            if not plans:
                return  # 收官：无 open 清单，watcher 退出
            changed = False
            for plan in plans:
                if plan.get("status") != store.PLAN_ACTIVE:
                    continue  # pending_confirm：等用户，不调度
                if await _reconcile(slug, session_id, plan):
                    changed = True
                if plan.get("status") == store.PLAN_CANCELLED:
                    await _stop_running_children(slug, session_id, plan)
            if changed:
                _schedule_flush(session_id)
            evt = _WAKE.get(session_id)
            try:
                if evt is not None:
                    evt.clear()
                    await asyncio.wait_for(evt.wait(), timeout=_TICK_S)
                else:
                    await asyncio.sleep(_TICK_S)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — watcher 挂了不能带走 sidecar
        _log.exception("plan_watcher_crashed session=%s", session_id)
    finally:
        _WATCHERS.pop(session_id, None)
        _WAKE.pop(session_id, None)
        _log.info("plan_watcher_stop session=%s", session_id)


# ---- 对账 + 调度 -----------------------------------------------------------

async def _reconcile(slug: str, session_id: str, plan: dict) -> bool:
    """一个 tick 对一张清单：先收终态，再补位调度。返回是否有状态变化。"""
    changed = False
    # 1) running 条目对账（子会话实况 → 终态）
    for it in list(store.running_items(plan)):
        st, summary, err = await _child_state(it.get("child_session_id"))
        if st in store.ITEM_TERMINAL:
            updated = store.mark_settled(
                slug, session_id, plan["plan_id"], it["item_id"], st,
                result_summary=summary, error=err,
            )
            if updated:
                changed = True
                _note(session_id, plan, it["item_id"], st, summary or err)
                _push(slug, session_id, updated)
                if updated.get("status") == store.PLAN_COMPLETED:
                    _note_complete(session_id, updated)
    if not changed:
        plan = store.get_plan(slug, session_id, plan["plan_id"]) or plan
    # 2) 补位调度（对账后重读——完成判定可能已翻状态）
    plan = store.get_plan(slug, session_id, plan["plan_id"]) or plan
    if plan.get("status") != store.PLAN_ACTIVE:
        return changed
    for it in store.ready_items(plan):
        ok = await _launch_item(slug, session_id, plan, it)
        if ok is None:  # 并发帽满：本 tick 到此为止
            break
        if ok:
            changed = True
    return changed


async def _child_state(child_id: str | None):
    """子会话实况 → (item 终态 | running, summary, error)。"""
    if not child_id:
        return store.ITEM_RUNNING, "", ""
    from ..session_meta import _find_meta

    meta, _ = _find_meta(child_id) or ({}, None)
    if not meta:
        # 子会话被删：视为 stopped（清单不该卡在一个不存在的会话上）
        return store.ITEM_STOPPED, "", "child session deleted"
    if meta.get("type") == "delegation":
        stop = meta.get("stop_reason") or ""
        if stop == "running" or not stop:
            return store.ITEM_RUNNING, "", ""
        sub = meta.get("subagent") or {}
        summary = (sub.get("result_summary") or "")[:400]
        if stop in ("error", "timeout"):
            return store.ITEM_FAILED, summary, stop
        return store.ITEM_DONE, summary, ""
    sub = meta.get("subagent") or {}
    st = sub.get("status") or "running"
    if st == "done":
        return store.ITEM_DONE, (sub.get("result_summary") or "")[:400], ""
    if st == "failed":
        return store.ITEM_FAILED, "", (sub.get("result_summary") or sub.get("error") or "failed")[:400]
    if st == "stopped":
        return store.ITEM_STOPPED, "", ""
    return store.ITEM_RUNNING, "", ""


async def _launch_item(slug: str, session_id: str, plan: dict, it: dict) -> bool | None:
    """启动一个条目。True=已启动；False=启动失败（条目已标 failed）；
    None=并发帽满（保持 pending，下轮再试）。"""
    backend = it.get("backend") or store.BACKEND_GINNO
    # 子代理 origin 沿清单作者：user split（/subagent 拆分确认）的子代理
    # origin=user（侧栏「由用户发起」徽标），agent 建单则 agent。
    origin = "user" if plan.get("author") == "user" else "agent"
    try:
        if backend == store.BACKEND_GINNO:
            from ..subagent_scheduler import create_subagent

            res = await create_subagent(
                session_id,
                it["goal"],
                it.get("constraints") or "",
                it.get("acceptance") or "",
                origin=origin,
                agent_type=it.get("agent_type") or "",
            )
            if res.get("ok"):
                child = res["session_id"]
            elif res.get("limit"):
                return None
            else:
                _fail_item(slug, session_id, plan, it, str(res.get("error") or "spawn failed"))
                return False
        else:
            child = await _launch_delegation(slug, session_id, plan, it, backend)
            if child is None:
                return None
            if child == "":
                return False  # 启动失败已在内部标记
    except Exception as e:  # noqa: BLE001 — 单条目失败不拖垮 watcher
        _log.exception("plan_item_launch_failed plan=%s item=%s", plan["plan_id"], it["item_id"])
        _fail_item(slug, session_id, plan, it, f"{type(e).__name__}: {e}")
        return False
    updated = store.mark_running(slug, session_id, plan["plan_id"], it["item_id"], child)
    if updated:
        _log.info(
            "plan_item_started plan=%s item=%s backend=%s child=%s",
            plan["plan_id"], it["item_id"], backend, child,
        )
        _push(slug, session_id, updated)
        return True
    return False


async def _launch_delegation(
    slug: str, session_id: str, plan: dict, it: dict, backend: str
) -> str | None:
    """条目走委托后端（D1）。返回 delegation 会话 id；"" = 启动失败（已
    标记）；None = 帽满。委托的并发帽复用 _active_delegations_cap。"""
    from ..tools.external_agent import (
        _BACKENDS,
        _TIMEOUT_DEFAULT_S,
        _active_delegations_cap,
        _delegation_bg,
    )
    from ..delegation_sessions import create_delegation

    be = _BACKENDS.get(backend)
    if be is None or not be.available():
        _fail_item(
            slug, session_id, plan, it,
            f"delegation backend unavailable: {backend}",
        )
        return ""
    if _active_delegations_cap() <= 0:
        return None
    from ..server_shared import _SESSIONS

    parent = _SESSIONS.get(session_id) or {}
    cwd = str(parent.get("primary_path") or parent.get("workspace") or "")
    prompt = it["goal"]
    if it.get("constraints"):
        prompt += f"\n\nConstraints:\n{it['constraints']}"
    if it.get("acceptance"):
        prompt += f"\n\nAcceptance:\n{it['acceptance']}"
    m = "edit" if it.get("mode") == "edit" else "read-only"
    did = create_delegation(
        parent_session_id=session_id,
        project_slug=slug,
        backend=backend,
        mode=m,
        prompt=prompt,
        workspace=cwd,
    )
    if not did:
        _fail_item(slug, session_id, plan, it, "create_delegation failed")
        return ""
    spawn_bg(
        _delegation_bg(did, session_id, slug, be, backend, prompt, m, cwd, _TIMEOUT_DEFAULT_S)
    )
    return did


def _fail_item(slug, session_id, plan, it: dict, error: str) -> None:
    """启动失败 → 条目 failed（无 plan 句柄时按 id 重读）。"""
    if plan is None:
        from .store import get_plan

        found = None
        for p in store.list_plans(slug, session_id):
            if any(x["item_id"] == it.get("item_id") for x in p.get("items") or []):
                found = p
                break
        plan = found
    if plan is None:
        return
    updated = store.fail_item(slug, session_id, plan["plan_id"], it["item_id"], error)
    if updated:
        _note(session_id, plan, it["item_id"], store.ITEM_FAILED, error)
        _push(slug, session_id, updated)


async def _stop_running_children(slug: str, session_id: str, plan: dict) -> None:
    """清单取消 → 停掉在跑的子会话（协作式；delegation 子会话受自身
    timeout 约束，v1 不强杀——条目等它自然终态后回写 stopped 语义由
    mark_settled 的终态覆盖）。"""
    from ..subagent_scheduler import stop_subagent_tree

    for it in store.running_items(plan):
        child = it.get("child_session_id")
        if not child:
            continue
        from ..session_meta import _find_meta as _fm

        meta, _ = _fm(child) or ({}, None)
        if meta and meta.get("type") == "delegation":
            continue  # v1：委托进程由 timeout 兜底，不强杀
        try:
            await stop_subagent_tree(child, include_self=True)
        except Exception:  # noqa: BLE001
            _log.exception("plan_cancel_stop_failed child=%s", child)


def _push(slug: str, session_id: str, plan: dict) -> None:
    """清单变化广播（前端 board 刷新用；P3 前无消费方也无妨）。"""
    spawn_bg(_push_session_event(session_id, "plan.updated", {"plan": plan}))


# ---- 去抖合流注入（D4）------------------------------------------------------

def _note(session_id: str, plan: dict, item_id: str, status: str, detail: str) -> None:
    buf = _PENDING_FLUSH.setdefault(
        session_id, {"first": time.monotonic(), "notes": [], "plans": set()}
    )
    buf["notes"].append(
        f"- [{status}] {item_id} {(detail or '')[:200]}".rstrip()
    )
    buf["plans"].add(plan.get("plan_id") or "")
    buf["last"] = time.monotonic()


def _note_complete(session_id: str, plan: dict) -> None:
    buf = _PENDING_FLUSH.setdefault(
        session_id, {"first": time.monotonic(), "notes": [], "plans": set()}
    )
    done = sum(1 for x in plan.get("items") or [] if x.get("status") == "done")
    fail = sum(1 for x in plan.get("items") or [] if x.get("status") == "failed")
    buf["notes"].append(
        f"plan {plan.get('plan_id')} COMPLETED: {done} done, {fail} failed"
    )
    buf["last"] = time.monotonic()


def _schedule_flush(session_id: str) -> None:
    """起/保活冲刷定时器：8s 静默或 30s 硬上限。asyncio 环境内调用。"""
    existing = _FLUSH_TASKS.get(session_id)
    if existing is not None and not existing.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _PENDING_FLUSH.pop(session_id, None)
        return
    _FLUSH_TASKS[session_id] = loop.create_task(_flush_timer(session_id))


_FLUSH_TASKS: dict[str, Any] = {}


async def _flush_timer(session_id: str) -> None:
    while True:
        buf = _PENDING_FLUSH.get(session_id)
        if buf is None:
            return
        age = time.monotonic() - buf["first"]
        wait = _FLUSH_QUIET_S if age + _FLUSH_QUIET_S <= _FLUSH_MAX_S else max(0.0, _FLUSH_MAX_S - age)
        await asyncio.sleep(wait)
        buf = _PENDING_FLUSH.get(session_id)
        if buf is None:
            return
        # 静默窗口到 或 硬上限到：冲刷
        if time.monotonic() - buf["first"] >= _FLUSH_MAX_S - 0.05 or _quiet(buf):
            _do_flush(session_id, buf)
            return


def _quiet(buf: dict) -> bool:
    """距最后一条 note ≥ 8s 视为静默（_note 时间戳记在 note 末尾的元组里
    太绕——用 buf["last"] 维护）。"""
    return time.monotonic() - buf.get("last", buf["first"]) >= _FLUSH_QUIET_S


def _do_flush(session_id: str, buf: dict) -> None:
    _PENDING_FLUSH.pop(session_id, None)
    _FLUSH_TASKS.pop(session_id, None)
    text = "\n".join(buf["notes"])
    _log.info("plan_wake_flush session=%s notes=%d", session_id, len(buf["notes"]))
    from ..subagent_scheduler import _inject_result
    from ..subagent_scheduler.brief import _xml_attr, _xml_text

    # 注入走子代理结果同通道（steer 或 idle 唤醒）；child key 用 plan-<id>。
    # 正文包一层专用 <ginno_plan_update> 信封（设计 §8 P3 专用卡）：模型侧
    # 保留 [plan update] 行动菜单文本，前端按信封折专用清单卡而非裸渲染。
    keys = sorted(p for p in buf["plans"] if p)
    child_key = f"plan-{keys[0]}" if keys else "plan"
    body = (
        "[plan update] Task list status changed:\n"
        f"{text}\n"
        "You can: verify the above results against acceptance; use "
        "amend_plan to reset/replace failed items (reset = retry); "
        "deliver the final report when all items are settled. Proceed "
        "autonomously unless a user decision is required."
    )
    envelope = (
        f'<ginno_plan_update plan="{_xml_attr(keys[0] if keys else "")}">\n'
        f"{_xml_text(body)}\n"
        "</ginno_plan_update>"
    )
    try:
        spawn_bg(_inject_result(session_id, envelope, child_key))
    except Exception:  # noqa: BLE001
        _log.exception("plan_wake_inject_failed session=%s", session_id)
