"""远端会话的 turn 执行器（remote-session-design.md R1）。

一个会话挂 ``remote_peer``（peer slug）后，它的 turn **在远端 peer 执行**，
本地不跑图。本模块是那条路径的唯一实现，A（peer 当子 agent）与
B（会话级归属远端）共用它。

事件契约：发与本地 turn **同名同形**的 WS 事件（turn.start / token.delta /
tool.start / tool.end / message.end / done），前端零改动即可渲染。

持久化：远端回复经 ``graph.aupdate_state`` 写回本地 checkpoint，
故 ``get_session_history`` 照常读到历史（它读 channel_values.messages）。

安全（设计 §4）：
- 对端离线/不可达 → **fail-fast 明确报错**，绝不静默回退本地执行
- 远端 trust/caps/exec_root 仍由**对端 guard** 强制，本地只是发起方
- 本地不代批远端权限请求（R4 接权限卡）
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ...server_shared import _log, _push_session_event

# 远端任务轮询间隔（降级模式：对端不支持 SSE 时）
_POLL_S = 3.0
_TERMINAL = ("completed", "failed", "canceled", "rejected", "input-required", "auth-required")


class RemoteTurnError(RuntimeError):
    """远端 turn 失败。fail-fast，绝不静默回退本地。"""


def remote_peer_of(session: dict) -> str:
    """会话是否挂远端 peer；返回 slug 或空串。"""
    return str(session.get("remote_peer") or "").strip()


def _resolve_peer(slug: str):
    from ...a2a.peers import PeerStore

    rec = PeerStore().find(slug)
    if rec is None:
        raise RemoteTurnError(f"远端 peer {slug!r} 不存在（可能已被移除）")
    if not rec.enabled:
        raise RemoteTurnError(f"远端 peer {rec.name!r} 已禁用")
    if not rec.endpoint:
        raise RemoteTurnError(f"远端 peer {rec.name!r} 未配置 endpoint")
    return rec


def _context_for(session_id: str, slug: str) -> tuple[Any, str]:
    """取/建本地 session ↔ 远端 contextId 映射（复用出站 contexts.json）。"""
    from ...a2a.contexts import ContextRecord, ContextMap, _key

    store = ContextMap()
    rec = store.get_or_none(_key(session_id, slug))
    if rec is None:
        rec = ContextRecord(ginno_sid=session_id, peer_slug=slug)
        store.set(rec)
    return store, rec.context_id or ""


def _client(rec):
    from ...a2a.client import A2AClient

    # 指纹钉住只对 https 有意义（明文由环回/隧道提供安全）；
    # client 内部已按 scheme 分流，这里照传即可。
    return A2AClient(
        endpoint=rec.endpoint,
        token=rec.token or None,
        verify_fp=rec.peer_fingerprint or None,
        timeout_s=float(rec.task_timeout_s or 600),
    )


def _sid(session: dict) -> str:
    """Session id: production dict uses ``session_id``; tests may use ``id``."""
    return str(session.get("session_id") or session.get("id") or "")


async def _persist_exchange(session: dict, user_text: str, reply: str) -> None:
    """把本轮问答写回本地 checkpoint，使 history 照常可读。

    远端会话本地不跑图，故必须显式追加消息；用 aupdate_state 走正规
    checkpoint 通道，不自己拼 jsonl（避免与切分/fold 语义打架）。
    """
    graph = session.get("graph")
    if graph is None or not reply:
        return
    try:
        from langchain_core.messages import AIMessage, HumanMessage

        cfg = {"configurable": {"thread_id": _sid(session)}}
        await graph.aupdate_state(
            cfg, {"messages": [HumanMessage(content=user_text), AIMessage(content=reply)]}
        )
    except Exception:  # noqa: BLE001 — 持久化失败不该吞掉已完成的远端结果
        _log.exception("remote_turn_persist_failed session=%s", session.get("id"))


async def run_remote_turn(
    session: dict,
    user_text: str,
    turn_id: str | None = None,
    *,
    agent_id: str | None = None,
) -> dict:
    """在远端 peer 上跑一轮，发兼容事件，回写本地历史。

    返回 ``{"ok": bool, "text": str, "state": str, "task_id": str, "error": str}``。
    **不抛**（除 peer 配置类错误）——失败也发 done，前端不会卡在 streaming。
    """
    sid = _sid(session)
    slug = remote_peer_of(session)
    tid = turn_id or uuid.uuid4().hex
    t0 = time.time()

    rec = _resolve_peer(slug)  # 配置错误直接抛（fail-fast，不静默回退）
    store, ctx_id = _context_for(sid, slug)
    client = _client(rec)

    await _push_session_event(
        sid, "turn.start",
        {"turn_id": tid, "agent_id": agent_id or "", "name": rec.name,
         "remote": True, "peer": slug},
        tid,
    )

    state, text, task_id, err = "", "", "", ""
    task = None
    try:
        # H2: if the last remote task parked on input-required, this follow-up
        # must reference it so the peer resumes that task instead of starting
        # a new one on a checkpoint that still has a pending interrupt.
        ref_ids: list[str] = []
        prev_tasks = []
        prev = store.get_or_none(sid, slug)
        if prev is not None:
            prev_tasks = list(prev.tasks)
            for tb in reversed(prev_tasks):
                if tb.state == "input-required":
                    ref_ids = [tb.task_id]
                    break

        task = await client.send_message(
            user_text,
            context_id=ctx_id or None,
            reference_task_ids=ref_ids or None,
        )
        task_id = task.id
        # 远端 contextId 落盘 → 后续轮次延续同一远端上下文。
        # M6: 不再整条覆盖——先读旧记录，保留 tasks 影子（peer 删除守卫
        # 和 delegate 的 referenceTaskIds 依赖它）。
        if task.context_id and task.context_id != ctx_id:
            from ...a2a.contexts import ContextRecord

            store.set(ContextRecord(
                ginno_sid=sid, peer_slug=slug, context_id=task.context_id,
                tasks=prev_tasks,
            ))
        state = str(task.status.state.value if hasattr(task.status.state, "value")
                    else task.status.state)
        await _push_session_event(
            sid, "notice",
            {"message": f"远端 {rec.name} 已接单（task {task_id[:8]}）", "kind": "remote"},
            tid,
        )
        # 轮询至终态，并把状态变化作为进度发出
        last = state
        deadline = t0 + float(rec.task_timeout_s or 600)
        while state not in _TERMINAL and time.time() < deadline:
            await asyncio.sleep(_POLL_S)
            task = await client.get_task(task_id)
            state = str(task.status.state.value if hasattr(task.status.state, "value")
                        else task.status.state)
            if state != last:
                last = state
                await _push_session_event(
                    sid, "notice",
                    {"message": f"远端状态：{state}", "kind": "remote"}, tid,
                )
        text = _task_text(task)
        if state not in _TERMINAL:
            err = f"远端任务超时（{rec.task_timeout_s}s 未达终态）"
            state = "failed"
    except Exception as exc:  # noqa: BLE001 — 网络/协议失败一律 fail-fast 报错
        err = f"{type(exc).__name__}: {exc}"
        state = "failed"
        _log.warning("remote_turn_failed session=%s peer=%s err=%s", sid, slug, err)

    # M8: 轮询超时 → best-effort 取消远端任务（否则占死对端并发额度）
    if state == "failed" and task_id and err and "timeout" in err.lower():
        try:
            await client.cancel_task(task_id)
        except Exception:  # noqa: BLE001 — best-effort
            pass

    # M7: 释放 httpx 连接池
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass

    # 产物内容（P1-1 已实现回传）作为工具结果呈现，前端复用既有渲染
    for art in _task_artifacts(task):
        await _push_session_event(
            sid, "tool.start", {"name": "remote_artifact", "id": art["id"]}, tid
        )
        await _push_session_event(
            sid, "tool.end", {"id": art["id"], "content": art["content"]}, tid
        )

    # 影子记账：更新 task 状态（H2 下一轮 ref_ids 查找 + peer 删除守卫）
    if task_id:
        from ...a2a.contexts import _key as _ctx_key
        store.update_task_state(_ctx_key(sid, slug), task_id, state or "unknown")

    if text:
        await _push_session_event(sid, "token.delta", {"content": text}, tid)
    if err:
        await _push_session_event(
            sid, "notice", {"message": f"远端执行失败：{err}", "kind": "error"}, tid
        )

    await _persist_exchange(session, user_text, text or (f"[远端执行失败] {err}" if err else ""))
    await _push_session_event(sid, "message.end", {}, tid)
    await _push_session_event(
        sid, "done", {"turn_id": tid, "remote": True, "state": state}, tid
    )
    return {"ok": not err, "text": text, "state": state, "task_id": task_id, "error": err}


def _task_text(task: Any) -> str:
    """从 task 的 status.message 取回复文本。"""
    if task is None:
        return ""
    msg = getattr(task.status, "message", None)
    if not msg:
        return ""
    parts = getattr(msg, "parts", None) or []
    return "\n".join(
        str(getattr(p, "text", "") or "") for p in parts if getattr(p, "text", None)
    ).strip()


def _task_artifacts(task: Any) -> list[dict]:
    """远端产物 → 前端可渲染的内容块（含 P1-1 回传的文本内容）。"""
    out: list[dict] = []
    if task is None:
        return out
    for art in getattr(task, "artifacts", None) or []:
        parts = getattr(art, "parts", None) or []
        # part[0] 是路径摘要，part[1:] 才是内容（P1-1 契约）
        body = "\n".join(
            str(getattr(p, "text", "") or "") for p in parts[1:] if getattr(p, "text", None)
        )
        head = str(getattr(parts[0], "text", "") or "") if parts else ""
        out.append({
            "id": getattr(art, "artifact_id", None) or uuid.uuid4().hex,
            "content": (head + ("\n\n" + body if body else "")).strip(),
        })
    return out
