"""``list_members(wait=True)`` 的阻塞等待原语（agent-team-design.md §5.3.2，P3）。

herdr 把「等某个 agent 到某个状态」做成一等原语（``agent wait --until
blocked``），这是它能被脚本和**其他 agent** 驱动的基础。Ginno 的对应实现
直接复用 ``list_subagents(wait=true)`` 的同一套语义：轮询 + 协作式打断
（调用方 session 的 ``_TURN_STOP``）+ 超时 partial。

§5.9 纪律：**状态只读 :mod:`.status`**（member_status），工具层与成员面板
读同一个服务端真值——herdr 能做到「agent 等到另一个 agent 真的 blocked
而不是盲发按键」，靠的正是这一点。
"""

from __future__ import annotations

import asyncio
import time

from ginno_runtime import jsonx as json

from ..lang import t
from ..server_shared import _TURN_STOP
from . import status as team_status
from . import store

# 「已安定」的语义态：等待的终点候选。working/waiting 是仍在产出，不算。
SETTLED_STATUSES = ("blocked", "unknown", "error", "done", "idle")

# 全部合法语义态（until 传错名字必须报错，否则默认 timeout=0 时永久死等）。
VALID_STATUSES = ("idle", "working", "blocked", "waiting", "done", "unknown", "error")

_POLL_S = 0.2


def _settled(status: str, until: list[str] | None) -> bool:
    """是否到达等待终点。

    ``idle`` 蕴含 ``done``（backlog P2-20）：done = 完成未读，用户看过工作台
    就转 idle。若 until=["done"] 只认字面 done，用户一查看就永不收敛——
    「用户看了结果」不该把 Lead 的等待卡死。
    """
    targets = until or SETTLED_STATUSES
    if status in targets:
        return True
    return status == "idle" and "done" in targets


async def wait_for_members(
    caller_session_id: str,
    team_id: str,
    until: list[str] | None = None,
    ids: list[str] | None = None,
    timeout_s: int = 0,
) -> str:
    """阻塞直到目标成员实例到达 ``until`` 中的任一语义态（默认 = 任何
    「已安定」态）。返回成员行 JSON；``timeout_s > 0`` 到点返回 partial 标记；
    调用方 turn 被用户停止时返回 ``[stopped]`` 标记并照常让 turn 收尾。

    ``ids`` 是 instance_id 列表（不是 agent_id——同 agent 可多实例）；
    空 = 团队全部成员。
    """
    deadline = (
        time.monotonic() + max(0, int(timeout_s or 0)) if (timeout_s or 0) > 0 else None
    )
    # until 枚举校验（backlog P2-22）：传错状态名会永不匹配，而默认
    # timeout_s=0 → 永久死等，是 Lead 场景的卡死形状。宁可立刻报错。
    if until:
        bad = [s for s in until if s not in VALID_STATUSES]
        if bad:
            return t(
                f"[error] Invalid until status: {', '.join(bad)}. "
                f"Valid: {', '.join(VALID_STATUSES)}",
                f"[error] until 状态名非法：{', '.join(bad)}。"
                f"合法值：{', '.join(VALID_STATUSES)}",
            )

    def _rows() -> list[dict]:
        team = store.get_team(team_id)
        if team is None:
            return []
        slug = str(team.get("project_slug") or "default")
        out = []
        for m in team.get("members") or []:
            if ids and m.get("instance_id") not in ids:
                continue
            st, detail = team_status.member_status(
                slug, m.get("session_id") or "", m, "main"
            )
            out.append({
                "instance_id": m.get("instance_id"),
                "display_name": m.get("display_name") or m.get("agent_id"),
                "agent_id": m.get("agent_id"),
                "status": st,
                "status_detail": detail,
            })
        return out

    if ids:
        known = {m.get("instance_id") for m in store.list_members(team_id)}
        missing = [i for i in ids if i not in known]
        if missing and not known.intersection(ids):
            # 一个都解析不到：永远等下去是死锁形状的 bug，直接报错。
            return t(
                f"[error] Member instance(s) not found: {', '.join(missing)}",
                f"[error] 未找到成员实例：{', '.join(missing)}",
            )

    while True:
        rows = _rows()
        # 团队已解散 ≠ 团队为空（backlog P1-10）：必须先判解散，否则会被
        # 下面「空团队」那条兜底当成正常返回，Lead 以为等完了其实队没了。
        if store.get_team(team_id) is None:
            return t(
                "[error] The team no longer exists; the wait was aborted.",
                "[error] 团队已不存在，等待中止。",
            )
        if not ids and not rows:
            return json.dumps({"partial": False, "members": []}, ensure_ascii=False)
        # 等待**过程中**目标成员被移除：rows 恒空而 ids 非空，上面那条兜底
        # 不触发 → 默认 timeout=0 时永久死循环，Lead 的 turn 挂死。
        if ids and not rows:
            return t(
                "[error] The waited member(s) no longer exist (team disbanded or "
                "member removed); the wait was aborted.",
                "[error] 等待的成员已不存在（团队被解散或成员被移除），等待中止。",
            )
        if rows and all(_settled(r["status"], until) for r in rows):
            return json.dumps(
                {"partial": False, "members": rows}, ensure_ascii=False
            )
        evt = _TURN_STOP.get(caller_session_id)
        if evt is not None and evt.is_set():
            return t(
                "[stopped] The user stopped the current turn; the wait was "
                "interrupted. Current member statuses:\n",
                "[stopped] 用户停止了当前回合，等待中断。当前成员状态：\n",
            ) + json.dumps({"partial": True, "members": rows}, ensure_ascii=False)
        if deadline is not None and time.monotonic() >= deadline:
            return t(
                "(partial) Wait timed out; not all members have settled:\n",
                "(partial) 等待超时，成员尚未全部安定：\n",
            ) + json.dumps({"partial": True, "members": rows}, ensure_ascii=False)
        await asyncio.sleep(_POLL_S)
