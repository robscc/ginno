"""Connection opening: delegation gate, session resolution, accept,
file watcher, pending-interrupt re-emit — everything that must happen
before the receive loop can safely start.

Split out of ``session_ws`` (batch 4-2, structural only — no behavior
change). Produces the :class:`WsConn` bundle (session, config, watcher,
send lock) consumed by the loop/handler modules, so connection-scoped
state has exactly one owner. Returns ``None`` when the connection was
fully handled (delegation replay pump / unknown session): the caller
simply returns — no teardown is owed, exactly like the pre-split early
returns that lived before the endpoint's ``try:``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from fastapi import WebSocket

from .... import files as files_mod
from .... import vibe_island as _vibe_island
from ....server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _ev,
    _log,
)
from ...sessions import _ensure_session, _start_goal_driver
from ..resume import resume_pending_interrupt


@dataclass
class WsConn:
    """Connection-scoped state shared by the loop/handler modules.

    Owns the file-watcher task (armed in :func:`open_connection`, torn
    down in :mod:`.closing`) — the former ``_ws_lock`` / ``_watch_stop`` /
    ``_watcher_task`` locals that used to span ~1200 lines of one function.
    """

    ws: WebSocket
    session_id: str
    session: dict
    config: dict
    send_lock: asyncio.Lock
    watch_stop: asyncio.Event
    watcher_task: "asyncio.Task[None]"

    def spawn_resume(self, resume_value: dict) -> None:
        """Forward to the shared public resume entry (api/stream/resume.py).

        Refactored (dingtalk-connector-design.md §4 F4): the peek/pop/spawn
        protocol now lives in one place; the desktop WS passes its captured
        turn config so behavior is byte-equivalent with the previous inline
        implementation. External channels call ``resume_pending_interrupt``
        directly with ``base_config=None``.
        """
        resume_pending_interrupt(self.session_id, resume_value, base_config=self.config)


async def open_connection(ws: WebSocket, session_id: str) -> WsConn | None:
    # Delegation 子会话是静态回放档案（external-agents-design.md §9）——
    # 无 graph、无 model，不接受 turn；否则 _ensure_session 会在深处的
    # model 解析上炸出一个莫名其妙的错。明确拒绝 + 提示回放只读。
    from ....session_meta import _find_meta

    _del_meta, _ = _find_meta(session_id) or ({}, None)
    if isinstance(_del_meta, dict) and _del_meta.get("type") == "delegation":
        # 回放会话：接受连接但保持打开——发错误后立刻 close 会触发前端
        # WS 自动重连风暴（每次重连又收到错误，2026-10-04 真机事故）。
        # 静默挂着；仅当客户端真的发来 invoke 时回一条错误，仍不关闭。
        await ws.accept()
        # 静默吞掉一切消息：回放会话 composer 已隐藏，真实调用不存在；而对
        # invoke 回 error 事件会让前端"关连接→重连→自动发消息→又收错误"
        # 循环刷屏（2026-10-05 真机，修复版仍在风暴）。连接保持打开即可。
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return

    session = _ensure_session(session_id)
    if not session:
        await ws.accept()
        # Event contract (i18n-design.md §3): English fallback text + i18n_key
        # + params — the frontend renders t(i18n_key, params) when present.
        await ws.send_text(
            _ev(
                "error",
                {
                    "message": f"unknown session: {session_id}",
                    "i18n_key": "stream.error_unknown_session",
                    "params": {"id": session_id},
                },
            )
        )
        await ws.close()
        return

    await ws.accept()
    _SESSION_WS.setdefault(session_id, []).append(ws)
    _log.info("ws_open session=%s agent=%s", session_id, session.get("agent_id"))
    graph = session["graph"]
    config = {"configurable": {"thread_id": session_id, "project_slug": session["project_slug"]}}

    # File watcher (docs §7.5): stat the session's registered files every 5s.
    # A changed mtime → preview.invalidate (the UI refreshes if that file is
    # open) + a stale badge on the artifact (cleared on next preview fetch).
    # Sends share _ws_lock so watcher frames never interleave with turn frames.
    _ws_lock = asyncio.Lock()
    _watch_stop = asyncio.Event()

    async def _file_watcher() -> None:
        reg = files_mod.get_registry(session["project_slug"])
        while not _watch_stop.is_set():
            try:
                await asyncio.wait_for(_watch_stop.wait(), timeout=5)
                break  # stop set
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            artifacts_dirty = False
            try:
                for e in reg.list_session(session_id):
                    try:
                        m = Path(e["path"]).stat().st_mtime
                    except OSError:
                        continue
                    if m != e.get("mtime", 0):
                        e["mtime"] = m
                        if not e.get("stale"):
                            reg.mark_stale(e["id"], True)
                            artifacts_dirty = True
                        async with _ws_lock:
                            await ws.send_text(
                                _ev(
                                    "preview.invalidate",
                                    {"file_id": e["id"], "reason": "mtime"},
                                )
                            )
                if artifacts_dirty:
                    async with _ws_lock:
                        await ws.send_text(_ev("artifacts.changed", {}))
            except Exception:
                continue

    _watcher_task = asyncio.create_task(_file_watcher())


    # Re-emit any permission interrupt left pending from a previous connection:
    # the graph pauses at permission_node awaiting a resume, so a reconnect or a
    # session switch mid-permission would otherwise orphan the turn (the prompt
    # is gone and there is no way to resume). The payload mirrors the
    # __interrupt__ handling in _run_stream below, so the client's existing
    # permission.request handler applies unchanged.
    try:
        # Read the parked interrupt straight off the checkpointer rather than
        # via graph.aget_state().tasks[].interrupts: that route needs pending
        # writes to be surfaced, and even then misses every interrupt raised
        # inside the tools node (ask_user, workflow_propose_edit) because
        # langgraph commits one extra write-less checkpoint on top of it. See
        # FileCheckpointer.get_pending_interrupt.
        _pending = graph.checkpointer.get_pending_interrupt(config)
        for intr in _pending or []:
            value = getattr(intr, "value", None) or intr
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            payload: dict | None = None
            if kind == "permission_request":
                payload = {"tool": value.get("tool"), "args": value.get("args")}
                event = "permission.request"
            elif kind == "version_propose":
                # A workflow-dev diff proposal pending at disconnect: re-show it
                # so the turn isn't orphaned (same resume channel as permission).
                payload = {
                    "workflow_id": value.get("workflow_id"),
                    "from_version": value.get("from_version"),
                    "diff": value.get("diff"),
                    "rationale": value.get("rationale"),
                }
                event = "version.propose"
            elif kind == "user_question":
                # An ask_user question parked at disconnect. The payload carries
                # the tool_call id, which is what lets the client MERGE this
                # re-emit into the block it already rebuilt from history instead
                # of showing the question twice.
                payload = {
                    "id": value.get("id"),
                    "question": value.get("question", ""),
                    "header": value.get("header", ""),
                    "options": value.get("options") or [],
                    "allow_free_text": value.get("allow_free_text", True),
                    # 多问题（契约 v2）：中断值里的 questions 原样透传。
                    "multi": bool(value.get("multi")),
                    "questions": value.get("questions") or [],
                }
                event = "user.question"
            if payload is None:
                continue
            # Re-arm the resume guard: after a runtime restart the in-memory
            # flags are gone even though the interrupt persists in the file.
            _PENDING_RESUME.add(session_id)
            _PENDING_KIND[session_id] = kind
            _RUNNING_TURNS.setdefault(session_id, "")
            await ws.send_text(_ev(event, payload))
            # Vibe Island 重新武装:parked 的 ask_user/审批提醒随旧进程没了
            # (VI 不跨重启恢复 custom 卡片),重连补发同一 Notification 让
            # 刘海重新显示「Needs approval」。同一 approval 重复提醒只是
            # 幂等的状态重设,无副作用。
            _vibe_island.on_turn_event(session_id, event, payload)
    except Exception:
        # introspecting resume state must never stop the socket from opening
        pass

    # Cross-restart goal resume (design §4.3.3): an active goal re-arms its
    # continuation driver as soon as the session is loaded again.
    _start_goal_driver(session_id)

    return WsConn(
        ws=ws,
        session_id=session_id,
        session=session,
        config=config,
        send_lock=_ws_lock,
        watch_stop=_watch_stop,
        watcher_task=_watcher_task,
    )
