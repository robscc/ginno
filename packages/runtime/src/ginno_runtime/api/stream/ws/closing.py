"""Disconnect cleanup: watcher teardown + broadcast-registry pruning.

Split out of ``session_ws`` (batch 4-2, structural only — no behavior
change). The teardown that used to live in the endpoint's ``finally``
block: stop the file-watcher task and drop this socket from
``_SESSION_WS`` so a dead entry cannot linger. State that must survive a
disconnect (checkpoints, parked interrupts, the steer stash) is persisted
by its owning layer long before this point — nothing extra is owed here.
"""

from __future__ import annotations

from fastapi import WebSocket

from ....server_shared import _SESSION_WS, _log
from .handshake import WsConn


def close_connection(ws: WebSocket, session_id: str, conn: WsConn) -> None:
    _log.info("ws_close session=%s", session_id)
    conn.watch_stop.set()
    conn.watcher_task.cancel()
    # Drop this socket from the broadcast registry (a dead entry would
    # otherwise linger until the next send attempt pruned it).
    _SESSION_WS[session_id] = [
        w for w in (_SESSION_WS.get(session_id) or []) if w is not ws
    ]
