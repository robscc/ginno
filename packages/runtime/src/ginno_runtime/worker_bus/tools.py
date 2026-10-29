"""Worker 协议的 model-facing 工具（worker-protocol-design.md §3/§4/§6）。

四个工具：list_workers（花名册）/ assign_worker（派活）/ watch_worker
（订阅）/ worker_status（状态查询，可阻塞等待）。命名对齐既有
list_subagents 家族。不进权限豁免集（同 spawn_subagent 先例——用户
批准的是派活本身）。无 session_id（workflow/列举端点）时为空表。
"""

from __future__ import annotations

import asyncio
import json as _json

from langchain_core.tools import tool

from ..lang import t
from ..server_shared import _log
from ..session_meta import _find_meta
from .deliver import assign_to_worker
from .registry import TERMINAL_PROJECTED, list_worker_rows, project_status, resolve_target
from .watch import watch_worker as _watch_impl

_LIST_DOC_EN = """List worker sessions you can assign to or watch — the roster
(subagents, delegations, scheduled runs, and other main conversations)
with live status: working / waiting / idle / done / failed / stopped /
unknown. Sorted by recent activity; the rows carry title, type, status
and (when done) a short result summary.

Args:
    query: substring matched against title or session id (case-insensitive).
    type_filter: one of subagent / delegation / scheduled / main (empty = all).
    status_filter: one of working / waiting / idle / done / failed / stopped
        / unknown (empty = all).
    limit: max rows (default 20, cap 50).
    all_projects: false to restrict to the current project (default: all
        projects — workers are addressed globally, rows carry a `project`
        field; Ginno de-emphasizes projects, so the roster matches what
        addressing sees).
"""

_LIST_DOC = """列出可派活/可订阅的 worker 会话花名册（子代理、外部委托、
定时执行、其它 main 会话），带实时状态：working / waiting / idle / done /
failed / stopped / unknown。按最近活动倒序；行内含标题、类型、状态与
（done 时的）结果摘要。

Args:
    query: 标题或 session id 的子串匹配（不分大小写）。
    type_filter: subagent / delegation / scheduled / main 之一（空 = 全部）。
    status_filter: working / waiting / idle / done / failed / stopped /
        unknown 之一（空 = 全部）。
    limit: 最多返回行数（默认 20，上限 50）。
    all_projects: false 时仅列当前项目（默认全部项目——寻址本就跨项目，
        行内带 ``project`` 字段标注归属；Ginno 项目概念已弱化，花名册与
        寻址同口径）。
"""

_ASSIGN_DOC_EN = """Assign a self-contained task to an EXISTING worker
session (do not use this to create new subagents — spawn_subagent does that).

The worker receives your goal/constraints/acceptance as a task brief; it
cannot see this conversation, so write for a capable colleague with ZERO
context. Delivery: the worker absorbs it in its current turn if busy, or is
woken with a fresh turn if idle. Receipt is one of:

* absorbed — committed into the worker's checkpoint (durable);
* queued — stashed for its next turn (NOT durable: a runtime crash drops it;
  verify with worker_status if the worker stays silent);
* refused — not delivered (target is terminal, or not an assignable
  session; live channel conversations cannot be assigned to).

A finished worker's report goes to its own parent automatically — you get it
only via watch_worker. Main conversations accept assignments (the user's
other chats act as workers), but they never settle: follow them with
watch_worker(want="digest").

Args:
    target: worker session id (from list_workers), or a title that uniquely
        matches one worker.
    goal: the complete, self-contained task description (required).
    constraints: boundaries (read-only scope, files not to touch, budget…).
    acceptance: criteria the result will be judged against.
"""

_ASSIGN_DOC = """给一个**已存在**的 worker 会话派自足任务（新建子代理请用
spawn_subagent，不要用本工具）。

worker 收到的是 goal/constraints/acceptance 组成的任务简报；它看不到
本对话——按「有能力但完全无上下文的同事」来写。投递：worker 在忙则
当前回合内吸收；空闲则被唤醒开新回合处理。回执三态：

* absorbed — 已提交进 worker 的 checkpoint（持久）；
* queued — 已入队等它下个回合（**不持久**：运行时崩溃会丢；worker 迟迟
  无响应时用 worker_status 查证）；
* refused — 未投递（worker 已终态，或目标不可派活；实时渠道会话不可被
  派活）。

已完工 worker 的报告会自动回传它自己的父会话——你要拿结果需先
watch_worker 订阅它。main 会话（用户的其它对话）可被派活，但它们没有
终态：跟踪进度用 watch_worker(want="digest")。

Args:
    target: worker 的 session id（来自 list_workers），或能唯一命中一个
        worker 的标题。
    goal: 自足的完整任务描述（必填）。
    constraints: 边界规则（只读范围、不许动的文件、时间预算…）。
    acceptance: 验收标准，收到结果后对照判定。
"""

_WATCH_DOC_EN = """Subscribe to a worker session's final output: when it
settles (done / failed / stopped), a result card is injected into THIS
conversation automatically — no polling. One-shot: the subscription fires
once and is removed. If the worker is already settled, the summary comes
back in this very call instead.

Use digest mode (want="digest") for a long-running worker: a throttled
status excerpt is injected as it progresses (at most once per 5 minutes),
and you still get the final result via the injection channel. Digest is
the ONLY mode for main conversations — they are ongoing and never settle.

Args:
    target: worker session id (from list_workers) or unique title.
    want: "terminal" (default — fire once when settled; task-type workers
        only) or "digest" (progress excerpts + final).
"""

_WATCH_DOC = """订阅一个 worker 会话的最终产出：它终态（done / failed /
stopped）时，结果卡自动注入**本对话**——无需轮询。一次性：触发即取消
订阅。若 worker 已终态，摘要直接随本次调用返回，不再注册。

digest 模式（want="digest"）适合长任务 worker：节流推送状态节选（最快
每 5 分钟一条），终态结果照常回传。main 会话只能用 digest——持续对话
永不安定（settle）。

Args:
    target: worker 的 session id（来自 list_workers）或唯一命中的标题。
    want: "terminal"（默认——终态一次性触发，仅限 task 型 worker）或
        "digest"（进度节选+终态）。
"""

_STATUS_DOC_EN = """Check one worker session's live status — or block until
it reaches a settled state. Statuses: working / waiting / idle / done /
failed / stopped / unknown. Returns the row from list_workers plus, when
done, the result summary.

Args:
    target: worker session id or unique title.
    wait: false = return immediately; true = block until settled (or
        timeout). Default false.
    timeout_s: max seconds to wait when wait=true (0 = wait indefinitely).
"""

_STATUS_DOC = """查一个 worker 会话的实时状态，或阻塞等它安定。状态：
working / waiting / idle / done / failed / stopped / unknown。返回
list_workers 的行，done 时附结果摘要。

Args:
    target: worker 的 session id 或唯一命中的标题。
    wait: false = 立即返回；true = 阻塞到安定（或超时）。默认 false。
    timeout_s: wait=true 时的最长等待秒数（0 = 一直等）。
"""


def build_worker_tools(session_id: str | None, project_slug: str | None = None):
    """空 session_id → 空表（workflow / 列举端点语义，同 subagent 工具）。"""
    if not session_id:
        return []

    async def list_workers(
        query: str = "",
        type_filter: str = "",
        status_filter: str = "",
        limit: int = 20,
        all_projects: bool = True,
    ) -> str:
        rows = list_worker_rows(
            project_slug,
            query=query,
            type_filter=type_filter,
            status_filter=status_filter,
            limit=limit,
            all_projects=all_projects,
        )
        if not rows:
            return t("No worker sessions match.", "没有匹配的 worker 会话。")
        return _json.dumps({"count": len(rows), "workers": rows}, ensure_ascii=False)

    async def assign_worker(
        target: str, goal: str, constraints: str = "", acceptance: str = ""
    ) -> str:
        r = await assign_to_worker(session_id, target, goal, constraints, acceptance)
        receipt = r.get("receipt")
        if receipt == "absorbed":
            msg = t("Task delivered (absorbed into the worker's checkpoint).",
                    "任务已投递（已提交进 worker 的 checkpoint）。")
        elif receipt == "queued":
            msg = t("Task queued for the worker's next turn (not durable — "
                    "verify with worker_status if it stays silent).",
                    "任务已入队等 worker 下个回合（不持久——迟迟无响应时用 "
                    "worker_status 查证）。")
        else:
            reason = r.get("reason") or ""
            detail = r.get("detail") or ""
            msg = t(f"[refused] {reason}", f"[未投递] {reason}")
            if detail:
                msg += "\n" + detail
            return msg
        return _json.dumps({"ok": True, "receipt": receipt, "msg_id": r.get("msg_id")},
                           ensure_ascii=False) + "\n" + msg

    async def watch_worker(target: str, want: str = "terminal") -> str:
        r = await _watch_impl(session_id, target, want)
        if not r.get("ok"):
            return t(f"[error] {r.get('error')}", f"[错误] {r.get('error')}")
        if r.get("already_settled"):
            return _json.dumps(
                {"already_settled": True, "status": r.get("status"),
                 "result_summary": r.get("result_summary")},
                ensure_ascii=False,
            )
        return _json.dumps({"watched": True, "status": r.get("status"),
                            "want": r.get("want")}, ensure_ascii=False)

    async def worker_status(
        target: str, wait: bool = False, timeout_s: int = 0
    ) -> str:
        tid, meta, err = resolve_target(target)
        if err or not meta:
            return t(f"[error] {err}", f"[错误] {err}")
        deadline = asyncio.get_event_loop().time() + max(0, int(timeout_s or 0))
        while True:
            found = _find_meta(tid)
            meta = found[0] if found else meta
            st = project_status(tid, meta)
            if not wait or st in TERMINAL_PROJECTED:
                row = {
                    "session_id": tid,
                    "title": meta.get("title"),
                    "status": st,
                }
                if st == "done":
                    row["result_summary"] = str(
                        (meta.get("subagent") or {}).get("result_summary") or ""
                    )[:400]
                return _json.dumps(row, ensure_ascii=False)
            if timeout_s and asyncio.get_event_loop().time() >= deadline:
                return _json.dumps({"session_id": tid, "status": st,
                                    "wait": "timeout"}, ensure_ascii=False)
            await asyncio.sleep(1.0)

    list_workers.__doc__ = t(_LIST_DOC_EN, _LIST_DOC)
    assign_worker.__doc__ = t(_ASSIGN_DOC_EN, _ASSIGN_DOC)
    watch_worker.__doc__ = t(_WATCH_DOC_EN, _WATCH_DOC)
    worker_status.__doc__ = t(_STATUS_DOC_EN, _STATUS_DOC)
    return [
        tool(list_workers),
        tool(assign_worker),
        tool(watch_worker),
        tool(worker_status),
    ]
