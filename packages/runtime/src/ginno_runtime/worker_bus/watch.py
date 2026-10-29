"""订阅（worker-protocol-design.md §6.2）：meta ``subscribers[]`` 注册 +
终态钩子回传 + digest（P2）+ 重启对账。

回传复用 ``_inject_result`` 的双路管道（steer 忙 / coalesce wake 闲），
天然继承：订阅者侧合并窗、终态订阅者拒绝、全部竞态防护。一次性语义
（want=terminal 触发即移除订阅）同时就是"已通知"的簿记——崩溃在翻转
与回传之间时订阅还在，启动对账据此补发。
"""

from __future__ import annotations

import time
from typing import Any

from .. import paths
from ..lang import t
from ..server_shared import _log, spawn_bg
from ..session_meta import (
    _find_meta,
    _session_meta_list,
    _session_meta_patch,
)
from ..subagent_scheduler import _inject_result
from ..subagent_scheduler.brief import _xml_attr, _xml_text
from .registry import (
    TASK_TYPES,
    TERMINAL_PROJECTED,
    _mtype,
    project_status,
    resolve_target,
)

SUBSCRIBER_CAP = 8
DIGEST_MIN_INTERVAL_S = 300  # 同一订阅者两次 digest 的最小间隔

# 进程内"被订阅的 session id"快查表：on_turn_settled 每回合都过一遍
# hook，不能为无订阅的会话付磁盘扫描。watch_worker 是唯一写者，
# reconcile_subscriptions 启动时重建。
_WATCHED: set[str] = set()


def _ancestors_of(meta: dict) -> set[str]:
    """沿 parent_session_id 上溯的祖先集（结构性回传已覆盖他们——订阅
    与血缘去重，防同一条结果注入两次）。"""
    out: set[str] = set()
    pid = meta.get("parent_session_id")
    seen: set[str] = set()
    while pid and pid not in seen:
        seen.add(pid)
        out.add(pid)
        found = _find_meta(pid)
        pid = found[0].get("parent_session_id") if found else None
    return out


async def watch_worker(
    subscriber_id: str, target: str, want: str = "terminal"
) -> dict[str, Any]:
    """订阅注册。目标已终态且 want=terminal 时不注册，直接把结果带回
    （工具返回值即回执，省一回合注入）。"""
    if want not in ("terminal", "digest"):
        return {"ok": False, "error": "want must be 'terminal' or 'digest'"}
    tid, meta, err = resolve_target(target)
    if err or not meta:
        return {"ok": False, "error": err or "unknown target"}
    # main 会话无终态（不会 done/failed/stopped），terminal 订阅永远不触发
    # ——只收 digest（每回合 settle 都过 on_turn_settled_hook，节流推送）。
    if want == "terminal" and _mtype(meta) not in TASK_TYPES:
        return {
            "ok": False,
            "error": t(
                "this session has no terminal state (it is an ongoing "
                "conversation); use want='digest' to follow its progress",
                "该会话是持续对话、没有终态；用 want='digest' 订阅它的进度",
            ),
        }
    found = _find_meta(tid)
    if not found:
        return {"ok": False, "error": "target meta missing"}
    _meta, slug = found
    st = project_status(tid, _meta)
    if want == "terminal" and st in TERMINAL_PROJECTED:
        summary = str((_meta.get("subagent") or {}).get("result_summary") or "")[:400]
        return {"ok": True, "already_settled": True, "status": st,
                "result_summary": summary}
    subs = [s for s in (_meta.get("subscribers") or []) if isinstance(s, dict)]
    now = int(time.time())
    entry = {"session_id": subscriber_id, "want": want, "since": now}
    # 幂等：重复 watch 刷新（digest 的 last_digest 保留——不因重新订阅
    # 绕过节流）
    kept = []
    for s in subs:
        if s.get("session_id") == subscriber_id and s.get("want") == want:
            entry["last_digest"] = s.get("last_digest") or 0
        else:
            kept.append(s)
    if len(kept) + 1 > SUBSCRIBER_CAP:
        return {"ok": False,
                "error": f"subscriber cap reached ({SUBSCRIBER_CAP})"}
    _session_meta_patch(slug, tid, {"subscribers": [*kept, entry]})
    _WATCHED.add(tid)
    _log.info("worker_watch sub=%s target=%s want=%s", subscriber_id, tid, want)
    return {"ok": True, "watched": True, "status": st, "want": want}


def _worker_result_text(target_id: str, meta: dict, status: str, digest: str = "") -> str:
    """``<ginno_worker_result>`` 信封（前端折员工回传卡）。digest 带标识
    属性；终态回传正文 = result_summary（done）/错误信息。"""
    title = str(meta.get("title") or target_id[:8])
    goal = str((meta.get("subagent") or {}).get("goal") or meta.get("title") or "")
    if digest:
        body = _xml_text(digest)
        kind = f' kind="digest"'
    elif status == "done":
        body = _xml_text(str((meta.get("subagent") or {}).get("result_summary") or ""))
        kind = ""
    else:
        body = _xml_text(
            str(meta.get("stop_reason") or status)
            if meta.get("type") == "delegation"
            else status
        )
        kind = ""
    return (
        f'<ginno_worker_result session="{_xml_attr(target_id)}"'
        f' from="{_xml_attr(title)}" status="{_xml_attr(status)}"'
        f' goal="{_xml_attr(goal[:120])}"{kind}>\n'
        f"{body}\n</ginno_worker_result>"
    )


async def notify_settled(target_id: str) -> None:
    """终态通知（幂等：先取走订阅者再投递，重入无害）。

    调用点：subagent gate 的 finalize（done/failed/stopped）、delegation
    的 finalize、启动对账。血缘祖先与目标自身不投（结构性回传已覆盖）。
    """
    found = _find_meta(target_id)
    if not found:
        return
    meta, slug = found
    subs = [s for s in (meta.get("subscribers") or []) if isinstance(s, dict)]
    if not subs:
        _WATCHED.discard(target_id)
        return
    st = project_status(target_id, meta)
    ancestors = _ancestors_of(meta)
    fire: list[str] = []
    for s in subs:
        if s.get("want") != "terminal":
            continue  # digest 订阅不因终态消耗（keep 统一收）
        sid = str(s.get("session_id") or "")
        if not sid or sid == target_id or sid in ancestors:
            continue  # 血缘/自身：结构性回传已覆盖，不投——但订阅照样消耗
        fire.append(sid)
    # 终态 = 所有 terminal 订阅一次性消耗（触发的投递、被血缘覆盖的、
    # 订阅者已删除的——否则血缘条目永远留在表里泄漏）。只留 digest。
    keep = [s for s in subs if s.get("want") != "terminal"]
    if keep:
        _WATCHED.add(target_id)
    else:
        _WATCHED.discard(target_id)
    if not fire:
        if keep != subs:
            _session_meta_patch(slug, target_id, {"subscribers": keep})
        return
        if keep != subs:
            _session_meta_patch(slug, target_id, {"subscribers": keep})
        return
    _session_meta_patch(slug, target_id, {"subscribers": keep})
    text = _worker_result_text(target_id, meta, st)
    _log.info("worker_notify_settled target=%s status=%s subs=%s",
              target_id, st, ",".join(fire))
    for sid in fire:
        # 复用注入管道：订阅者忙 → steer；闲 → coalesce wake。回执层级
        # 不追踪（协议 §11-2 一段式决策）。
        try:
            await _inject_result(
                sid, text, target_id,
                extra_kwargs={"ginno_worker_msg": {
                    "from": target_id, "kind": "result", "sent_at": int(time.time()),
                }},
            )
        except Exception:
            _log.exception("worker_notify_failed target=%s sub=%s", target_id, sid)


async def on_turn_settled_hook(session_id: str) -> None:
    """on_turn_settled 的前置钩子：digest 订阅者的节流推送。

    只碰 _WATCHED 里的会话（无订阅的 settle 零成本直接返回）。digest
    文本 = 状态 + 最近 assistant 文本节选（读 checkpoint 太重，这里用
    result_summary 之外的轻量途径：直接读最后一条 AIMessage 的前 300
    字——仅对被订阅的会话付这一次 IO）。
    """
    if session_id not in _WATCHED:
        return
    found = _find_meta(session_id)
    if not found:
        return
    meta, slug = found
    subs = [s for s in (meta.get("subscribers") or []) if isinstance(s, dict)]
    digests = [s for s in subs if s.get("want") == "digest"]
    if not digests:
        return
    now = int(time.time())
    due = [
        s for s in digests
        if now - int(s.get("last_digest") or 0) >= DIGEST_MIN_INTERVAL_S
    ]
    if not due:
        return
    st = project_status(session_id, meta)
    excerpt = await _last_assistant_excerpt(slug, session_id)
    body = f"[{st}] {excerpt}" if excerpt else f"[{st}]"
    text = _worker_result_text(session_id, meta, st, digest=body)
    keep = []
    for s in subs:
        if s in due:
            s = {**s, "last_digest": now}
        keep.append(s)
    _session_meta_patch(slug, session_id, {"subscribers": keep})
    _log.info("worker_digest target=%s subs=%d", session_id, len(due))
    for s in due:
        sid = str(s.get("session_id") or "")
        if not sid or sid == session_id:
            continue
        try:
            await _inject_result(
                sid, text, session_id,
                extra_kwargs={"ginno_worker_msg": {
                    "from": session_id, "kind": "digest", "sent_at": now,
                }},
            )
        except Exception:
            _log.exception("worker_digest_failed target=%s sub=%s", session_id, sid)


async def _last_assistant_excerpt(slug: str, session_id: str, limit: int = 300) -> str:
    """最近一条 assistant 文本的前 ``limit`` 字（digest 节选）。"""
    try:
        from langchain_core.messages import AIMessage

        from ..checkpointer import FileCheckpointer
        from ..graph import text_of_content

        tup = await FileCheckpointer(slug).aget_tuple(
            {"configurable": {"thread_id": session_id}}
        )
        messages = (
            ((tup.checkpoint.get("channel_values") or {}).get("messages") or [])
            if tup
            else []
        )
        for m in reversed(messages):
            if isinstance(m, AIMessage):
                text = text_of_content(getattr(m, "content", "")).strip()
                if text:
                    return text[:limit]
    except Exception:
        _log.info("worker_digest_excerpt_failed session=%s", session_id)
    return ""


def reconcile_subscriptions() -> None:
    """启动对账（server lifespan 调用）：扫描全部 meta——

    * terminal + 仍有 terminal 订阅 → 补发（崩溃在翻转与回传之间错过的）；
    * 重建 _WATCHED 快查表。
    派发为后台任务：lifespan 里不能 await 网络性回传（可能开 wake turn）。
    """
    targets: list[str] = []
    for p in paths.home().glob("projects/*/sessions/_index.json"):
        for m in _session_meta_list(p.parent.parent.name):
            subs = [s for s in (m.get("subscribers") or []) if isinstance(s, dict)]
            if not subs:
                continue
            sid = str(m.get("id") or "")
            _WATCHED.add(sid)
            if project_status(sid, m) in TERMINAL_PROJECTED:
                targets.append(sid)
    if not targets:
        return
    _log.info("worker_reconcile pending=%d", len(targets))

    async def _run() -> None:
        for sid in targets:
            await notify_settled(sid)

    spawn_bg(_run())
