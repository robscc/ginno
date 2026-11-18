"""远端会话的 turn 执行器（remote-session-design.md R1）。

一个会话挂 ``remote_peer``（peer slug）后，它的 turn **在远端 peer 执行**，
本地不跑图。本模块是那条路径的唯一实现，A（peer 当子 agent）与
B（会话级归属远端）共用它。

事件契约：发与本地 turn **同名同形**的 WS 事件（turn.start / token.delta /
tool.start / tool.end / message.end / done），前端零改动即可渲染。
2026-11-17 起优先走 A2A ``message/stream``（SSE）：节点侧事件汇把引擎帧
写进进度 JSONL（``kind="turn_event"``），这里逐帧转发——远端过程实时可见。
**frame_session 必须改写为本地 sid**：引擎给每帧盖的是远端会话 id，前端
会丢弃 frame_session 不等于本 socket 会话 id 的帧（2026-10-01 防跨会话
渲染泄漏守卫）——不改写则一帧都不渲染。

持久化：远端回复经 ``graph.aupdate_state`` 写回本地 checkpoint，
故 ``get_session_history`` 照常读到历史（它读 channel_values.messages）。

安全（设计 §4）：
- 对端离线/不可达 → **fail-fast 明确报错**，绝不静默回退本地执行
- 远端 trust/caps/exec_root 仍由**对端 guard** 强制，本地只是发起方
- 本地不代批远端权限请求（R4 接权限卡）

降级（绝不因流式不可用而让远端会话瘫痪）：
- 建流失败（对端不支持 streaming / 网络错误）→ 回落阻塞 ``send_message``；
- 中途断流 / 流提前收（任务已在对端跑）→ 拿 task id 回落 ``tasks/get`` 轮询，
  **绝不重复派活**；已流式推过的文本只补未推的尾部，不重复灌全文。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from ...a2a.errors import A2AError
from ...a2a.models import Task
from ...server_shared import _log, _push_session_event

# 远端任务轮询间隔（降级模式：对端不支持 SSE 时）
_POLL_S = 3.0
_TERMINAL = ("completed", "failed", "canceled", "rejected", "input-required", "auth-required")

# 生命周期事件由本地执行器自己发（turn.start 带 remote/peer 字段、done 带本地
# 状态），远端同名帧转发会重复/抢跑——抑制。其余 turn_event（tool.* /
# token.delta / thinking.* / usage / notice / permission.request …）一律转发。
# message.end/done 目前不经引擎 safe_send（到不了事件汇），列上是防御未来。
_SUPPRESS_EVENTS = frozenset({"turn.start", "turn.stopped", "message.end", "done"})

# 需要播 notice 的远端状态（2026-11-17）：只有「要用户动作」或「异常」才播。
# completed/canceled 由 turn 收口表达；working/submitted 由实时工具流表达。
_NOTIFY_STATES = frozenset({"input-required", "auth-required", "failed", "rejected"})


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


def _note_progress(sid: str, tid: str, *, tool_name: str | None = None,
                   agent_tail: str | None = None) -> None:
    """best-effort 喂遗言 WAL（远端 turn 进行中）。绝不抛。"""
    try:
        from ...lastwords import note_remote_progress

        note_remote_progress(sid, tid, tool_name=tool_name, agent_tail=agent_tail)
    except Exception:  # noqa: BLE001
        pass


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

    # ── 生命周期账本登记（2026-11-18 修复）───────────────────────────────
    # 出站远程 turn 此前不进本地生命周期：note_turn_start 不被调（它在
    # engine._stream_graph，远端路径顶部拦截走不到）、非 ws 调用方
    # （deliver/channel/scheduler）也不登记 _RUNNING_TURNS——dev-swap
    # 遗言 prepare 遍历账本时只能拿到残留的过期条目（实锤：抓到上一轮
    # 的「hi」turn，真正在途的远端任务完全不可见，重启后零恢复）。
    # 现在与本地 turn 同口径：登记账本 + WAL 遗言护体。覆盖式登记顺带
    # 收编任何历史残留（新 turn 结束时会正常清理）。
    from ...server_shared import _RUNNING_TURNS, _RUNNING_TURNS_AT

    _RUNNING_TURNS[sid] = tid
    _RUNNING_TURNS_AT[sid] = time.time()
    try:
        from ...lastwords import note_remote_turn_start

        note_remote_turn_start(
            sid, tid,
            str(session.get("project_slug") or "") or None,
            user_text,
            peer=slug,
        )
    except Exception:  # noqa: BLE001 — 遗言绝不拖累 turn 本体
        _log.exception("remote_turn_lastwords_start_failed session=%s", sid)

    state, text, task_id, err = "", "", "", ""
    try:
        task = None
        streamed_parts: list[str] = []  # 已流式推给前端的 token.delta 片段
        saw_token_delta = False
        accepted = False  # 接单 notice 只发一次
        deadline = t0 + float(rec.task_timeout_s or 600)
        try:
            # H2: if the last remote task parked on input-required, this follow-up
            # must reference it so the peer resumes that task instead of starting
            # a new one on a checkpoint that still has a pending interrupt.
            ref_ids: list[str] = []
            prev_tasks = []
            # ContextMap.get_or_none 只收**单个已构造的 key**（contexts.py:143），
            # 原实现误传 (sid, slug) 两参 → TypeError，远端会话每轮必崩。
            # 与下面 update_task_state 处同口径先 _key(sid, slug)。
            from ...a2a.contexts import _key as _prev_key

            prev = store.get_or_none(_prev_key(sid, slug))
            if prev is not None:
                prev_tasks = list(prev.tasks)
                for tb in reversed(prev_tasks):
                    if tb.state == "input-required":
                        ref_ids = [tb.task_id]
                        break

            async def _bookkeep(t: Any) -> None:
                """status 帧 / send_message 响应共用记账：task id、contextId 落盘、
                接单 notice（一次）、状态变化 notice。与旧阻塞路径同语义。"""
                nonlocal task, task_id, state, accepted, ctx_id
                task = t
                if getattr(t, "id", ""):
                    task_id = t.id
                # 远端 contextId 落盘 → 后续轮次延续同一远端上下文。
                # M6: 不再整条覆盖——先读旧记录，保留 tasks 影子（peer 删除守卫
                # 和 delegate 的 referenceTaskIds 依赖它）。
                if t.context_id and t.context_id != ctx_id:
                    from ...a2a.contexts import ContextRecord

                    store.set(ContextRecord(
                        ginno_sid=sid, peer_slug=slug, context_id=t.context_id,
                        tasks=prev_tasks,
                    ))
                    ctx_id = t.context_id
                new_state = str(t.status.state.value if hasattr(t.status.state, "value")
                                else t.status.state)
                if not accepted and task_id:
                    accepted = True
                    # 显示精简（2026-11-17 用户反馈）：原文案
                    # 「远端 X 已接单（task 4c9a469c）」有三处噪声——
                    # ① peer 名与会话头「远端 · X」徽标重复；② task id 是
                    # 开发向信息，用户不关心；③ 硬编码中文没走 i18n。
                    # 现改为 i18n_key + params，task_id 仍留在 payload 供排查。
                    await _push_session_event(
                        sid, "notice",
                        {"message": f"Remote {rec.name} accepted the task",
                         "i18n_key": "stream.remote_accepted",
                         "params": {"peer": rec.name},
                         "task_id": task_id,
                         "kind": "remote"},
                        tid,
                    )
                elif new_state != state and new_state in _NOTIFY_STATES:
                    # 只播**需要用户动作或异常**的状态。completed/canceled/
                    # working/submitted 一律不播：turn 收口与实时工具流本身
                    # 已经表达了这些，再播一条「远端状态：completed」是纯噪声
                    # （用户截图实录）。input-required 必须播——它告诉用户
                    # 「远端在等你回话」，是唯一需要用户动作的状态。
                    await _push_session_event(
                        sid, "notice",
                        {"message": f"Remote state: {new_state}",
                         "i18n_key": "stream.remote_state",
                         "params": {"state": new_state},
                         "kind": "remote"}, tid,
                    )
                state = new_state

            # 1) 优先流式：message/stream 逐帧消费（对端不支持时回落阻塞）。
            #    只捕获 A2AError（client 把一切网络/协议失败规整成它）——自身
            #    bug（TypeError 等）绝不吞进降级分支伪装成对端故障（假绿教训）。
            stream_fn = getattr(client, "stream_message", None)
            if stream_fn is not None:
                gen = stream_fn(
                    user_text,
                    context_id=ctx_id or None,
                    reference_task_ids=ref_ids or None,
                )
                try:
                    async for frame in gen:
                        if time.time() > deadline:
                            err = f"远端任务超时（{rec.task_timeout_s}s 未达终态）"
                            state = "failed"
                            break
                        fkind = frame.get("event")
                        fdata = frame.get("data") or {}
                        # 心跳 stamp：远端长任务不该触发 turn_gate_busy 的
                        # 20min staleness 自愈误 heal（帧到达即证明活着）。
                        _RUNNING_TURNS_AT[sid] = time.time()
                        if fkind == "status":
                            await _bookkeep(Task.from_dict(fdata))
                        elif fkind == "progress" and fdata.get("kind") == "turn_event":
                            ev = str(fdata.get("event") or "")
                            if not ev or ev in _SUPPRESS_EVENTS:
                                continue
                            payload = dict(fdata.get("data") or {})
                            # ⚠️ 关键改写：远端引擎盖的 frame_session 是**远端
                            # 会话 id**，前端守卫会丢弃 ≠ 本地 sid 的帧。
                            # _push_session_event 的 {"frame_session": sid, **data}
                            # 里 data 优先——必须在 data 里显式改写，光靠 push
                            # 侧盖章会被远端值覆盖。turn_id 同理换成本地 tid。
                            payload["frame_session"] = sid
                            payload["turn_id"] = tid
                            if ev == "token.delta":
                                saw_token_delta = True
                                chunk = str(payload.get("content") or "")
                                streamed_parts.append(chunk)
                                _note_progress(sid, tid, agent_tail=chunk)
                            elif ev == "tool.start":
                                # 工具边界 = 本地 note_tool_boundary 的对位点：
                                # bump effort（恢复价值门槛）+ 节流 WAL 覆盖写。
                                _note_progress(sid, tid, tool_name=str(payload.get("name") or ""))
                            await _push_session_event(sid, ev, payload, tid)
                except A2AError as exc:
                    if task is None:
                        # 建流失败（对端不支持 streaming / 鉴权 / 网络）→ 回落
                        # 阻塞派活。此时对端未受理任务，send_message 不会重复派活。
                        _log.info(
                            "remote_stream_unavailable_fallback session=%s peer=%s err=%s",
                            sid, slug, exc,
                        )
                        task = await client.send_message(
                            user_text,
                            context_id=ctx_id or None,
                            reference_task_ids=ref_ids or None,
                        )
                        await _bookkeep(task)
                    else:
                        # 中途断流：任务已在对端跑，绝不重复派活——下面轮询兜底。
                        _log.warning(
                            "remote_stream_broken_poll_fallback session=%s peer=%s task=%s err=%s",
                            sid, slug, task_id, exc,
                        )
                finally:
                    await gen.aclose()
            else:
                # 注入的 client 无流式能力（旧 fake / 旧实现）→ 直接阻塞路径。
                task = await client.send_message(
                    user_text,
                    context_id=ctx_id or None,
                    reference_task_ids=ref_ids or None,
                )
                await _bookkeep(task)

            # 2) 未达终态 → 轮询兜底（覆盖：阻塞回落 / 中途断流 / 对端空闲收流 /
            #    submitted 占位态）。流式正常到终态时此循环直接跳过。
            last = state
            while task_id and not err and state not in _TERMINAL and time.time() < deadline:
                await asyncio.sleep(_POLL_S)
                _RUNNING_TURNS_AT[sid] = time.time()  # 心跳（同流式循环）
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
            if not err and state not in _TERMINAL:
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

        # 终稿文本协调：流式已推过 token.delta 就**不再重复灌全文**（会重复渲染）。
        # - 从未流式推过文本（旧节点无事件汇 / 纯工具轮）→ 照旧一次性灌全文；
        # - 流式推过且终稿以其为前缀（中途断流回落轮询的场景）→ 只补未推的尾部；
        # - 流式推过且终稿一致/分叉 → 不补（分叉时宁缺毋重，历史仍落终稿全文）。
        streamed = "".join(streamed_parts)
        if not text and streamed:
            # 终态 task 无 status.message（回复只经 delta 下发）→ 用已流式内容兜底
            text = streamed
        if text:
            if not saw_token_delta:
                await _push_session_event(sid, "token.delta", {"content": text}, tid)
            elif text.startswith(streamed) and len(text) > len(streamed):
                await _push_session_event(
                    sid, "token.delta", {"content": text[len(streamed):]}, tid
                )
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
    finally:
        # ── 账本收口（2026-11-18 加固）──────────────────────────────────
        # 原实现的清理段在**可抛调用**（事件推送、影子记账、终稿补发）之后
        # 且无 finally 保护——任何一处抛异常或协程被取消（优雅关闭的 loop
        # teardown）都会跳过清理，_RUNNING_TURNS 残留过期条目，正是
        # 「dev-swap 遗言抓到上一轮 hi」实锤的残留来源之一。清理必须与
        # 执行体同生命周期。
        #
        # **远端一律清，不像本地那样为 interrupt 保留**（2026-11-17 语义）：
        # 远端没有任何东西在本机跑，续跑就是用户再发一条消息（H2 带
        # referenceTaskIds 让对端 resume 那个 task）。身份校验后再清，
        # 绝不删掉后继 turn 的登记。turn 正常结束时 note_remote_turn_end
        # 删 WAL 遗言；优雅关闭中（mark_shutdown）不删——SIGTERM 场景
        # write_lastwords(signal) 刚写好的遗言正等着新进程消费。
        try:
            from ...server_shared import _TURN_STOP

            if _RUNNING_TURNS.get(sid) == tid:
                _RUNNING_TURNS.pop(sid, None)
                _RUNNING_TURNS_AT.pop(sid, None)
            _TURN_STOP.pop(sid, None)
            from ...lastwords import note_remote_turn_end

            note_remote_turn_end(sid, tid)
        except Exception:  # noqa: BLE001 — 收口清理不得影响回执
            _log.exception("remote_turn_unregister_failed session=%s", sid)


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
