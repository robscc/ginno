"""聚合徽标端点 — Dock 数字 = 未读会话 + 未读定时任务 run + 团队 needs。

桌面壳（apps/desktop/src/badge.rs）每 5s 轮询本端点并把 ``needs`` 镜像到
macOS Dock。纪律与 /api/teams/badge 相同：零刷新直读已持久化状态——徽标
轮询要的是廉价，不是新鲜（三个分量各自的写入方负责落盘）：

- sessions   session_unread.unread_count()（直读各 project _index.json）
- schedule_runs schedule_store.unseen_summary()（mtime 键控 memo）
- teams      teams/status.rollup（roster 持久化语义态，blocked + done 未读）

``needs`` 字段名沿用 /api/teams/badge 的契约，Rust 壳的字符串 scrape 不变。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schedule_store, session_unread
from ..teams import status as team_status
from ..teams import store as team_store

router = APIRouter()


@router.get("/api/badge")
async def badge() -> dict:
    sessions = session_unread.unread_count()
    schedule_runs = schedule_store.unseen_summary().get("total", 0)
    blocked = 0
    done_unread = 0
    for t in team_store.list_teams():
        r = team_status.rollup(t)
        blocked += r.get("blocked", 0)
        done_unread += r.get("done_unread", 0)
    teams = blocked + done_unread
    return {
        "ok": True,
        "needs": sessions + schedule_runs + teams,
        "sessions": sessions,
        "schedule_runs": schedule_runs,
        "teams": teams,
        "blocked": blocked,
        "done_unread": done_unread,
    }
