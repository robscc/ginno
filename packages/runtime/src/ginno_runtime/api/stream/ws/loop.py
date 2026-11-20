"""The WebSocket endpoint + its receive loop (batch 4-2 split of
``session_ws``).

The loop module OWNS ``ws.receive_text()`` — the single message pump.
Branch handlers receive the parsed ``msg`` plus the :class:`WsConn` bundle
and can no longer touch the socket's receive side, so no branch can stall
or steal frames from another. Lightweight stateless branches (probes,
pings, mod UI forwarding) stay inline; stateful families live in
:mod:`.invoke` / :mod:`.steer` / :mod:`.control`.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .... import jsonx as json
from ....server_shared import _RUNNING_TURNS, _ev, _log
from .control import (
    handle_plan_cancel,
    handle_plan_confirm,
    handle_retry_from_checkpoint,
    handle_stop,
)
from .closing import close_connection
from .handshake import WsConn, open_connection
from .invoke import handle_invoke
from .steer import (
    handle_permission_response,
    handle_steer,
    handle_steer_recall,
    handle_user_answer,
)

router = APIRouter()


@router.websocket("/api/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    conn = await open_connection(ws, session_id)
    if conn is None:
        return
    try:
        await run_receive_loop(conn)
    except WebSocketDisconnect:
        return
    finally:
        close_connection(ws, session_id, conn)


async def run_receive_loop(conn: WsConn) -> None:
    ws = conn.ws
    session_id = conn.session_id
    while True:
        raw = await ws.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send_text(
                _ev(
                    "error",
                    {"message": "invalid JSON", "i18n_key": "stream.error_invalid_json"},
                )
            )
            continue

        kind = msg.get("type")
        if kind == "invoke":
            await handle_invoke(conn, msg)
        elif kind == "steer.recall":
            await handle_steer_recall(conn, msg)
        elif kind == "steer":
            await handle_steer(conn, msg)
        elif kind == "permission_response":
            await handle_permission_response(conn, msg)
        elif kind == "user_answer":
            await handle_user_answer(conn, msg)
        elif kind == "subagent.plan.confirm":
            await handle_plan_confirm(conn, msg)
        elif kind == "subagent.plan.cancel":
            await handle_plan_cancel(conn, msg)
        elif kind == "stop":
            await handle_stop(conn, msg)
        elif kind == "turn_state":
            # Post-reconnect probe (frontend ChatStream): is a turn still
            # streaming (or parked at an interrupt) for this session? If
            # not, the client reconciles against /history instead of
            # waiting on a stream that will never resume. ``compacting``
            # (2026-11-16 P5)：E3 摘要飞行中为 true——重连后前端据此
            # 保住「压缩中」镜像（否则任何事件都会清掉它，还在压缩的
            # turn 提前解锁 Composer）。
            try:
                from ....server_shared import _COMPACTING

                _compacting = session_id in _COMPACTING
            except Exception:  # noqa: BLE001 — 探针辅助
                _compacting = False
            try:
                await ws.send_text(
                    _ev(
                        "turn.state",
                        {
                            "running": session_id in _RUNNING_TURNS,
                            "turn_id": _RUNNING_TURNS.get(session_id, ""),
                            "compacting": _compacting,
                        },
                    )
                )
            except Exception:
                return  # socket died between recv and send
        elif kind == "mod.ui.press":
            # Mod band button (design §7.2): forward to the mods channel;
            # the broker validates the generation against its current
            # drawing and routes to the owning mod's runner. Disconnected
            # → silent no-op (a press on a dead band is not an error).
            _action_id = msg.get("actionId")
            if isinstance(_action_id, str) and _action_id:
                from ....mods.channel import get_channel

                try:
                    await get_channel().notify(
                        "ui.press",
                        {
                            "generation": int(msg.get("generation") or 0),
                            "actionId": _action_id,
                        },
                        session=session_id,
                    )
                except Exception as e:  # noqa: BLE001 — mods are additive
                    _log.debug("mod.ui.press dropped session=%s: %s", session_id, e)
        elif kind == "mod.ui.answer":
            # $.ui.ask 的回答(claude-code-mods-design.md §7.3):前端在
            # ModAskCard 上作答后回传,这里转发给 broker resolve 对应的
            # ask Promise(id 用 mod.ask args 里的字符串 id,原样回传)。
            # 晚到的 answer 是常态而非异常:broker 断连/60s 超时后 pending
            # 已被扫掉,回 not-found(ok=false)——静默吞掉,不当错误抛。
            _ask_id = msg.get("id")
            if isinstance(_ask_id, str) and _ask_id:
                from ....mods.channel import get_channel
                from ....mods.ops import OpError

                try:
                    await get_channel().call(
                        "broker",
                        "answer",
                        {"id": _ask_id, "value": msg.get("value")},
                        timeout=5.0,
                    )
                except OpError as e:
                    _log.debug("mod.ui.answer rejected session=%s: %s", session_id, e)
                except Exception as e:  # noqa: BLE001 — mods are additive
                    _log.debug("mod.ui.answer dropped session=%s: %s", session_id, e)
        elif kind == "ping":
            try:
                await ws.send_text(_ev("pong", {}))
            except Exception:
                return  # socket died between recv and send
        elif kind == "client_diag":
            # Client-reported anomaly channel (permanent): the packaged
            # webview has no readable console, so the client reports its
            # own detection (frame-ownership mismatch, stream landing in a
            # foreign turn's bubble) and it lands in sidecar.log where it
            # can actually be read. Rare by construction — see the guard in
            # useChatStreamEngine.
            _log.warning(
                "client_diag session=%s kind=%s detail=%s",
                session_id, msg.get("diag_kind"), str(msg.get("detail"))[:400],
            )
        elif kind == "retry_from_checkpoint":
            await handle_retry_from_checkpoint(conn, msg)
        else:
            try:
                await ws.send_text(
                    _ev(
                        "error",
                        {
                            "message": f"unknown type: {kind}",
                            "i18n_key": "stream.error_unknown_type",
                            "params": {"type": str(kind)},
                        },
                    )
                )
            except Exception:
                return
