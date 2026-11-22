"""聚合徽标端点 — Dock 数字 = 未读会话 + 未读定时任务 run + 团队 needs。

桌面壳（apps/desktop/src/badge.rs）每 5s 轮询本端点并把 ``needs`` 镜像到
macOS Dock。纪律与 /api/teams/badge 相同：零刷新直读已持久化状态——徽标
轮询要的是廉价，不是新鲜（三个分量各自的写入方负责落盘）：

- sessions   session_unread.unread_count()（直读各 project _index.json）
- schedule_runs schedule_store.unseen_summary()（mtime 键控 memo）
- teams      teams/status.rollup（roster 持久化语义态，blocked + done 未读）
- running    live_running_turns()（进程内登记表经三信号活性验证直读：
  leaked 条目就地 heal——纯内存 O(n)，n=并发 turn 数，无磁盘 IO）
- pinned_sessions session_unread.pinned_sessions()（同源 _index.json 直读，
  ≤8 条 id+title，menubar 托盘「置顶会话」快速导航区专用——见 desktop tray.rs）

``needs`` 字段名沿用 /api/teams/badge 的契约，Rust 壳的字符串 scrape 不变。
分层纪律：Dock badge 只管 ``needs``（要人看的），``running``（正在流式的
turn 数，主会话/subagent/team member 并发登记，>1 是正常值）只进 macOS
menubar（badge.rs → tray set_title）——working stays inside the app。
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import schedule_store, server_shared, session_unread
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
        # 归档团队不计入全局徽标（backlog P2-8，与 /api/teams/badge 同口径）。
        if t.get("archived"):
            continue
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
        # 正在流式的 turn 数（跨 session 可并发）；sidecar 进程内真源，无落盘。
        # 经活性验证读（2026-11-22 僵死 turn 事故）：泄漏条目就地 heal，
        # 僵死 turn 不再永久计入 menubar「运行中」。O(n) 纯内存，无磁盘 IO。
        "running": len(server_shared.live_running_turns(via="badge")),
        # 置顶会话（id+title ≤8 条）：托盘菜单快速导航用；老壳不认识该字段，
        # serde 端 default 兼容。见 session_unread.pinned_sessions。
        "pinned_sessions": session_unread.pinned_sessions(8),
    }
