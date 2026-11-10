"""dev-swap 热切换进度端点（docs/dev-swap-progress-design.md §2）。

两条进度来源统一到一个通道：
- ``POST /api/devswap/progress`` — scripts/dev_swap.sh 的 curl 上报（仅 127.0.0.1）；
- ``commands/registry._dev_swap_background`` 的逐行 stdout 解析（进程内直调
  :func:`update_progress`，与 curl 上报以「签名去重」合并，见下）。

状态快照存在 ``server_shared._DEVSWAP_PROGRESS``（进程级单例 dict）；
每次**对外可见**的状态变化向所有会话广播 ``runtime.devswap_progress``：
阶段切换/终态即时，build_runtime 内的 pct/detail 刷新 2s 节流。

事件/GET 契约（前端 ``normalizeDevSwapRun`` 只认扁平字段）：广播与快照都
带 :func:`_flat_view` 的 current-line——顶层 ``run_id/sid/stage/state/detail/
pct/log_tail/ts/started_at``。run 级 state 映射：任一阶段 fail → ``fail``；
finish ok/rolled_back → 同名终态；其余 ``running``。

终态闭环：``fail`` 由 shell ``die_stage`` 直接上报（旧进程还活着）；但
**成功/回滚的 finish 终态只有重启后的新进程有资格上报**（旧进程已被换掉、
且内存快照随之消失）——:func:`watch_swap_finish` 在新进程启动后盯
``dev-override/state.json`` 的 pending → verified/rolled_back* 跳变补上这一笔。

去重：curl 通道与 stdout 解析通道会对同一事件各报一次（shell 先打
``[devswap-progress]`` 行、再 curl），以
``(run_id, stage, state, detail, pct)`` 签名去重——签名与上次落库完全
相同的更新不重复广播。
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import server_shared

router = APIRouter()

# stage 枚举固定六值（design §3 表），顺序即阶段序。
STAGES = ("quiesce", "unit", "build_runtime", "preflight", "switch", "finish")
STATES = ("running", "ok", "fail", "rolled_back")
# 终态集合（幂等/回退判据用）：rolled_back 仅 finish 阶段合法（壳收尾回滚）。
_TERMINAL = ("ok", "fail", "rolled_back")

# build_runtime 内 pct/detail 刷新的广播节流窗口。
_BROADCAST_MIN_INTERVAL_S = 2.0

_last_broadcast_at = 0.0
# 上一条**落库**的签名（跨通道去重键）。
_last_signature: tuple | None = None


class ProgressReport(BaseModel):
    run_id: str = Field(min_length=1)
    stage: str
    state: str
    detail: str | None = None
    pct: int | None = Field(default=None, ge=0, le=100)
    log_tail: str | None = None
    # 发起会话（/dev-swap 命令路径才有；make dev-swap 终端路径为空）。
    # 前端据此做 §4.5 会话门控（B 会话不打扰），落块也只投发起会话。
    sid: str | None = None


def _new_run(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "stages": {s: {"state": "pending"} for s in STAGES},
        "current_stage": None,
        "sid": None,
        "started_at": time.time(),
        "updated_at": time.time(),
    }


def _should_broadcast_now(*, immediate: bool) -> bool:
    now = time.monotonic()
    if not immediate and (now - _last_broadcast_at) < _BROADCAST_MIN_INTERVAL_S:
        return False
    return True


def _broadcast_if_due(snapshot: dict, *, immediate: bool) -> None:
    """向所有会话广播快照。阶段切换/终态即时，build 内刷新 2s 节流。"""
    global _last_broadcast_at
    if not _should_broadcast_now(immediate=immediate):
        return
    _last_broadcast_at = time.monotonic()
    # fire-and-forget：无订阅会话时 no-op；调度失败绝不影响上报。
    try:
        loop = __import__("asyncio").get_running_loop()
    except RuntimeError:
        return
    loop.create_task(
        server_shared._push_global_event(
            "runtime.devswap_progress", {"ok": True, "run": _flat_view(snapshot)}
        )
    )


def update_progress(report: ProgressReport) -> dict:
    """落库一条进度上报，返回 {changed, broadcast} 供测试与调用方断言。

    状态机（design §8）：任一 stage 只允许 pending → running → ok/fail，
    终态后同 stage 的后续更新被忽略（幂等）；换 run_id 即开新 run（快照
    重置）。签名去重：与上次落库完全相同 → 只返回 changed=False。
    """
    global _last_signature, _last_broadcast_at
    state = server_shared._DEVSWAP_PROGRESS
    snap = state.get("snapshot")
    if snap is None or snap.get("run_id") != report.run_id:
        snap = _new_run(report.run_id)
        state["snapshot"] = snap
        _last_signature = None

    sig = (report.run_id, report.stage, report.state, report.detail, report.pct)
    if sig == _last_signature:
        return {"changed": False, "broadcast": False, "duplicate": True}
    _last_signature = sig

    slot = snap["stages"].get(report.stage)
    prev_state = slot["state"] if slot else "pending"
    if slot is not None:
        if prev_state in _TERMINAL:
            # 终态幂等：重复上报同一终态不再变更。
            if prev_state == report.state:
                snap["updated_at"] = time.time()
                return {"changed": False, "broadcast": False}
            # 不允许从终态回退（除非换 run）。
            return {"changed": False, "broadcast": False, "rejected": True}
    else:
        return {"changed": False, "broadcast": False, "rejected": "unknown_stage"}

    # sid 只在首个非空上报时落（stdout 解析通道每行都带；curl 通道不带）。
    if report.sid and not snap.get("sid"):
        snap["sid"] = report.sid

    slot.update(
        state=report.state,
        detail=report.detail,
        pct=report.pct,
        log_tail=report.log_tail,
        updated_at=time.time(),
    )
    snap["current_stage"] = report.stage
    snap["updated_at"] = time.time()

    # 广播策略：阶段切换（stage 或 running↔终态变化）即时；同 stage 内的
    # running 刷新（pct/detail）节流。
    immediate = report.state != "running" or prev_state != "running"
    if prev_state == "pending" and report.state == "running":
        immediate = True  # 首次进入新阶段
    _broadcast_if_due(snap, immediate=immediate)
    if immediate:
        _last_broadcast_at = time.monotonic()
    return {"changed": True, "broadcast": True}


def _flat_view(snap: dict) -> dict:
    """快照 → 事件/GET 的扁平 current-line（前端契约，见模块 docstring）。

    stage 取 current_stage（快照新建的空 run 回落最后一个非 pending 阶段）；
    run 级 state 只在「任一阶段 fail」或「finish 终态」时收敛，其余一律
    running——中间阶段的 ok 只是过场，shell 紧跟就报下一阶段 running。
    附带 stages→state 的精简映射（卡片时间线可直接用，缺省字段前端容错）。
    """
    stages: dict = snap["stages"]
    failed = next((s for s in STAGES if stages[s]["state"] == "fail"), None)
    if failed is not None:
        stage, state = failed, "fail"
    else:
        stage = snap.get("current_stage") or next(
            (s for s in reversed(STAGES) if stages[s]["state"] != "pending"), STAGES[0]
        )
        slot_state = stages[stage]["state"]
        state = (
            slot_state
            if stage == "finish" and slot_state in ("ok", "rolled_back")
            else "running"
        )
    slot = stages[stage]
    return {
        "run_id": snap["run_id"],
        "sid": snap.get("sid"),
        "stage": stage,
        "state": state,
        "detail": slot.get("detail"),
        "pct": slot.get("pct"),
        "log_tail": slot.get("log_tail"),
        "ts": snap.get("updated_at"),
        "started_at": snap.get("started_at"),
        "stages": {s: stages[s]["state"] for s in STAGES},
    }


def get_snapshot() -> dict:
    """当前 run 快照（GET）；无 run 时返回空壳。"""
    snap = server_shared._DEVSWAP_PROGRESS.get("snapshot")
    if snap is None:
        return {"ok": True, "run": None}
    return {"ok": True, "run": _flat_view(snap)}


@router.post("/api/devswap/start")
async def devswap_start(request: Request, body: dict | None = None) -> dict:
    """侧栏「热更新」按钮入口（2026-11-05）：无会话发起的 dev-swap。

    repo 解析顺序与 /dev-swap 命令一致：body.repo 显式 > settings
    context.dev_swap_repo > build-info.repo。运行中（当前 run 处于 running
    态）返回 409。构建进度经既有 progress 通道广播；完成事件由
    _dev_swap_background 以**全局广播**发出（无 sid），确认门照常弹出。
    """
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1"):
        return {"ok": False, "error": "local-only"}

    cur = server_shared._DEVSWAP_PROGRESS.get("snapshot") or {}
    if cur.get("state") == "running":
        return JSONResponse({"ok": False, "error": "already_running"}, status_code=409)

    import asyncio
    from pathlib import Path as _P

    from ..commands.registry import _dev_swap_background, _DEV_SWAP_TASKS
    from ..world_state import context_settings

    body = body or {}
    repo = str(body.get("repo") or "").strip()
    if not repo:
        repo = str(context_settings().get("dev_swap_repo") or "")
    if not repo:
        from .._build import build_info

        repo = str(build_info().get("repo") or "")
    if not repo or not (_P(repo) / "scripts" / "dev_swap.sh").exists():
        return JSONResponse(
            {"ok": False, "error": "repo_not_found"}, status_code=422
        )
    task = asyncio.create_task(_dev_swap_background("", repo))
    _DEV_SWAP_TASKS.add(task)
    task.add_done_callback(_DEV_SWAP_TASKS.discard)
    # 记住当前 run 的 task（cancel 用）；同 run 只有一个。
    server_shared._DEVSWAP_PROGRESS["task"] = task
    return {"ok": True, "repo": repo}


@router.post("/api/devswap/cancel")
async def devswap_cancel(request: Request) -> dict:
    """取消进行中的热更新（2026-11-05：左上按钮二击取消）。kill 构建子进程
    树并把进度标 cancelled；未到 switch 阶段取消零风险（symlink 未动）。"""
    client = request.client
    host = client.host if client else ""
    if host not in ("127.0.0.1", "::1"):
        return {"ok": False, "error": "local-only"}
    task = server_shared._DEVSWAP_PROGRESS.get("task")
    if not task or task.done():
        return {"ok": False, "error": "not_running"}
    task.cancel()
    return {"ok": True}


@router.post("/api/devswap/progress")
async def post_progress(report: ProgressReport, request: Request) -> dict:
    # 仅本机：dev_swap.sh / 本机脚本的上报通道，不暴露给局域网。
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "local-only"}
    if report.stage not in STAGES:
        return {"ok": False, "error": f"invalid stage: {report.stage}"}
    if report.state not in STATES:
        return {"ok": False, "error": f"invalid state: {report.state}"}
    return {"ok": True, **update_progress(report)}


@router.post("/api/lastwords/resume")
async def lastwords_resume(body: dict) -> dict:
    """确认续跑（2026-11-10 产品化：auto 模式人工会话的待确认流）。

    前端收 ``runtime.lastwords_pending`` 后引导用户进会话；用户确认续跑
    时调用本端点（body ``{"session_id": "…"}``）。dismiss=true 表示放弃。
    """
    from .. import lastwords as _lw

    sid = str((body or {}).get("session_id") or "")
    if not sid:
        return {"ok": False, "error": "session_id required"}
    if body.get("dismiss"):
        return _lw.dismiss_pending(sid)
    snap = _lw.pending_resume_snapshot(sid)
    if not snap:
        return {"ok": False, "error": "no_pending"}
    return await _lw.resume_pending(sid)


@router.post("/api/devswap/lastwords/prepare")
async def lastwords_prepare(request: Request, body: dict | None = None) -> dict:
    """遗言预告（docs/lastwords-design.md §1）：switch 段杀进程前的显式落盘。

    dev_swap.sh 在切换 symlink 前 curl 本端点（容错：sidecar 不在就跳过，
    SIGTERM 兜底仍在）；body 可带 ``fingerprint_old``（旧 bundle 指纹，随
    快照记录——恢复注入的消息里可辨识旧版本）。返回写入的活跃 turn 数。
    """
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "local-only"}
    import asyncio

    from .. import lastwords as _lastwords

    fp_old = str((body or {}).get("fingerprint_old") or "")
    # write_lastwords 是同步 IO（checkpoint JSONL 读取 + 落盘）；to_thread
    # 避免阻塞事件循环（正在流的 turn 不被 prepare 卡帧）。
    n = await asyncio.to_thread(
        _lastwords.write_lastwords, _lastwords.REASON_DEV_SWAP, fingerprint_old=fp_old
    )
    return {"ok": True, "count": n, "mode": _lastwords.mode()}


@router.get("/api/devswap/progress")
async def get_progress() -> dict:
    return get_snapshot()


# --------------------------------------------------------------------------- #
# 壳收尾终态 watcher（design §3 finish 行 + §4.4 结果落块的数据源）
# --------------------------------------------------------------------------- #

# state.json 终态 → finish 阶段上报 state 的映射。
_FINISH_STATE_MAP = {
    "verified": "ok",
    "rolled_back": "rolled_back",
    "rolled_back_factory": "rolled_back",
}


async def watch_swap_finish(poll_s: float = 2.0, budget_s: float = 1800.0) -> None:
    """新进程启动后盯 ``dev-override/state.json``：pending → verified/
    rolled_back* 的跳变即 finish 阶段终态，补上旧进程报不了的最后一笔。

    触发链：shell 写 state=pending → 壳 restart_sidecar（本进程随之启动）→
    壳验指纹/回滚后写终态 → 本 watcher 上报 finish ok/rolled_back 并广播。
    闸门「本次启动后见过 pending」：普通重启时 state 早已是历史终态，不重复报。
    """
    home = Path(os.environ.get("GINNO_HOME") or Path.home() / ".ginno")
    path = home / "dev-override" / "state.json"
    seen_pending = False
    deadline = time.monotonic() + budget_s
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            try:
                st = _json.loads(path.read_text())
            except Exception:  # noqa: BLE001 — 无文件/写到一半都当「还没有」
                continue
            state = st.get("state")
            if state == "pending":
                seen_pending = True
                continue
            if not seen_pending or state not in _FINISH_STATE_MAP:
                continue
            run_id = str(st.get("new") or "")
            if not run_id:
                return
            finish = _FINISH_STATE_MAP[state]
            update_progress(
                ProgressReport(
                    run_id=run_id,
                    stage="finish",
                    state=finish,
                    detail=(
                        f"verified {st.get('sha', '')}".rstrip()
                        if finish == "ok"
                        else "new bundle failed verification; previous bundle restored"
                    ),
                    sid=str(st.get("session_id")) if st.get("session_id") else None,
                )
            )
            return
    except asyncio.CancelledError:
        pass
