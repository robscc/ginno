"""Minimal Chrome DevTools Protocol client (one WebSocket per tab).

M1 helpers talk CDP, not the HTTP ``/json/new?url`` shortcut. The session is
owned by ``ChromeEngine`` and is not a public API.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any

from websockets.sync.client import connect

log = logging.getLogger(__name__)


class CdpError(RuntimeError):
    pass


class CdpSession:
    """Threaded CDP session. ``call`` is synchronous; events land on a queue."""

    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._events: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._closed = False

    def connect(self) -> None:
        self._ws = connect(self.ws_url, open_timeout=8, close_timeout=2, max_size=16 * 1024 * 1024)
        self._closed = False
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._closed = True
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def alive(self) -> bool:
        return not self._closed and self._ws is not None

    def _read_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            while not self._closed:
                try:
                    raw = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                except Exception:
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    q = None
                    with self._lock:
                        q = self._pending.pop(int(msg["id"]), None)
                    if q is not None:
                        q.put(msg)
                elif isinstance(msg, dict):
                    self._events.put(msg)
        except Exception:
            log.debug("cdp reader stopped", exc_info=True)
        finally:
            self._closed = True

    def call(self, method: str, params: dict | None = None, timeout: float = 12) -> dict[str, Any]:
        if self._ws is None or self._closed:
            raise CdpError(f"cdp session closed ({method})")
        with self._lock:
            self._id += 1
            msg_id = self._id
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._pending[msg_id] = waiter
            payload = {"id": msg_id, "method": method, "params": params or {}}
            try:
                self._ws.send(json.dumps(payload))
            except Exception as e:
                self._pending.pop(msg_id, None)
                raise CdpError(f"{method} send failed: {e}") from e
        try:
            msg = waiter.get(timeout=timeout)
        except queue.Empty as e:
            with self._lock:
                self._pending.pop(msg_id, None)
            raise CdpError(f"{method} timed out after {timeout}s") from e
        if msg.get("error"):
            err = msg["error"]
            text = err.get("message") if isinstance(err, dict) else str(err)
            raise CdpError(f"{method}: {text}")
        result = msg.get("result")
        return result if isinstance(result, dict) else {}

    def wait_event(self, method: str, timeout: float = 15) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ev = self._events.get(timeout=min(0.4, max(0.05, deadline - time.time())))
            except queue.Empty:
                if self._closed:
                    raise CdpError(f"cdp closed while waiting for {method}") from None
                continue
            if ev.get("method") == method:
                return ev
        raise CdpError(f"timed out waiting for {method}")

    def drain_events(self, limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while len(out) < limit:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out
