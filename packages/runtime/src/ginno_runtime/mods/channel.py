"""ModChannel — the Python↔broker side of the mods bus (design §5.2/§6).

One asyncio Unix-socket client for the Rust ``ginno-mod-broker``: newline
JSON frames (§6.1: call/result/event/next/catch-call/notify). Owns:

- connection lifecycle: env-injected socket (desktop/Tauri) or a dev-spawned
  broker binary (bridge_utils); hello handshake pushes the mods config,
  reconnects with exponential backoff and re-pushes it;
- event dispatch: per-session serial queues (cross-session parallel), each
  event awaiting the broker's chain-settle result within its deadline —
  timeout / disconnection / no-broker all resolve with the ORIGINAL payload
  (observe semantics: a mods hiccup can never stall a turn);
- Python-backed op serving: incoming ``call`` frames run ops.handle and
  answer with a result frame;
- notify fan-out: bands.update / toast / status / mod lifecycle → WS events.

Zero-overhead contract: when the channel is not connected, or the broker's
ready payload says no mod registered the event, dispatch returns without
touching any I/O.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

log = logging.getLogger("ginno.mods")

PROTOCOL_VERSION = 1

# Python waits deadlineMs + this grace before giving up on the broker's
# chain-settle reply (the broker enforces the deadline itself; the grace
# covers IPC + its report writing).
_DEADLINE_GRACE_S = 2.0

# hello handshake must not hang the first event longer than this.
# First hello spawns every configured runner in parallel; the broker caps
# each at HANDSHAKE_TIMEOUT (20 s), so this must exceed it or a slow-loading
# mod flaps the channel (runner tests: cold node + TS stripping can take s).
_HELLO_TIMEOUT_S = 30.0

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0

# P0 WS events (design §6.1): the notify methods the broker sends us and the
# WS event each maps to. ``mod.status`` (lifecycle) is special-cased below —
# it updates the local cache and rides ``mod.state.changed``.
_NOTIFY_TO_WS = {
    "bands.update": "mod.bands",       # session-scoped
    "toast": "mod.toast",              # global
    "status": "mod.ui.status",         # global ($ ui.status line; avoids the
    #                                      lifecycle ``mod.status`` collision)
}


class ChannelNotConnected(RuntimeError):
    """A Python→broker call was attempted with no live connection."""


class FrameError(ValueError):
    """A frame arrived malformed (bad JSON / not an object)."""


# ---- frame codec (newline JSON, design §6.1) --------------------------------

def encode_frame(frame: dict) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def decode_frame(line: bytes | str) -> dict:
    frame = json.loads(line)
    if not isinstance(frame, dict):
        raise FrameError("frame is not an object")
    return frame


async def _open_socket(sock_path: str) -> tuple[Any, Any]:
    return await asyncio.open_unix_connection(sock_path)


class ModChannel:
    """See module docstring. One instance per process (module singleton)."""

    def __init__(
        self,
        *,
        sock_path: str | None = None,
        token: str | None = None,
        open_connection: Any = None,
        hello_timeout: float = _HELLO_TIMEOUT_S,
        backoff_start: float = _BACKOFF_START_S,
        backoff_max: float = _BACKOFF_MAX_S,
    ) -> None:
        # Explicit socket (tests / env override); otherwise every connect
        # attempt re-resolves env → dev-spawned broker via bridge_utils.
        self._sock_path = sock_path
        self._token = token
        self._open_connection = open_connection or _open_socket
        self._hello_timeout = hello_timeout
        self._backoff_start = backoff_start
        self._backoff_max = backoff_max

        self._status = "disabled"  # disabled|connecting|connected|reconnecting|unavailable
        self._status_detail = ""
        self._task: asyncio.Task | None = None
        self._closing = False

        self._writer: Any = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)

        # Event names some mod registered (from the hello ``ready`` result);
        # None = unknown → always send. ``*`` and ``<ns>.*`` entries match
        # per createOn semantics (design §15.3).
        self._registered: set[str] | None = None
        # Broker-reported mod lifecycle states (name → {status, detail, ...}).
        self.mods_state: dict[str, dict] = {}

        # Per-session serial dispatch (design §6.1): one queue + one drain
        # task per session; sessions run in parallel.
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    # ---- lifecycle ------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == "connected" and self._writer is not None

    def active_for(self, event: str) -> bool:
        """Fast-path gate for the event taps: True only when dispatching
        ``event`` could actually reach a mod. Pure checks — no I/O, no await
        cost beyond the call itself (design §5.3 零开销 rule)."""
        if self._closing or self._status != "connected":
            return False
        return self._event_registered(event)

    def _event_registered(self, event: str) -> bool:
        reg = self._registered
        if reg is None:
            return True  # broker gave no hook info → let it short-circuit
        if "*" in reg or event in reg:
            return True
        ns = event.split(".", 1)[0]
        return f"{ns}.*" in reg

    def ensure_started(self) -> None:
        """Spawn the connect loop (idempotent, lazy — the taps and the API
        call this; a user with no mods never pays for the feature)."""
        if self._closing or (self._task and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (shutdown edge / sync import): stay lazy
        from .bridge_utils import load_mods_settings, scan_installed_mods

        cfg = load_mods_settings()
        if not cfg.get("enabled", True):
            self._status = "disabled"
            return
        # Zero-mod zero-cost rule (§5.3): nothing installed and no settings
        # item → never spawn/connect the broker. The next install (API or a
        # manual drop-in + first event) re-enters here.
        if not (cfg.get("items") or scan_installed_mods()):
            self._status = "disabled"
            self._status_detail = "no mods installed"
            return
        self._status = self._status if self._status != "disabled" else "connecting"
        self._task = loop.create_task(self._connect_loop())

    async def stop(self) -> None:
        self._closing = True
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ChannelNotConnected("channel stopped"))
        self._pending.clear()
        for sid, worker in list(self._workers.items()):
            self._queues[sid].put_nowait(None)  # drain-loop exit sentinel
        for worker in list(self._workers.values()):
            try:
                await asyncio.wait_for(worker, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                worker.cancel()
        self._workers.clear()
        self._queues.clear()
        from .bridge_utils import stop_broker

        await stop_broker()

    # ---- connection loop ----------------------------------------------------

    async def _connect_loop(self) -> None:
        backoff = self._backoff_start
        while not self._closing:
            sock_path, token = await self._resolve_endpoint()
            # The hello below reads self._token: carry what the endpoint
            # resolved (dev-spawn reads the token file; env/ctor paths set
            # it here already) — without this the default path sends null.
            if token:
                self._token = token
            if self._closing:
                return
            if sock_path is None:
                # No env socket and no broker binary discoverable: disabled
                # until a settings change / install restarts the loop
                # (external-agents 同款「未安装」态,不空转重试).
                self._set_status("unavailable", self._status_detail)
                return
            try:
                reader, writer = await self._open_connection(sock_path)
            except (OSError, Exception) as e:
                self._set_status("reconnecting", f"{type(e).__name__}: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue
            backoff = self._backoff_start
            try:
                await self._run_connection(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect below
                log.warning("mods channel connection error: %s: %s", type(e).__name__, e)
            if self._closing:
                return
            self._teardown_connection()
            self._set_status("reconnecting", "connection lost")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._backoff_max)

    async def _resolve_endpoint(self) -> tuple[str | None, str | None]:
        """Socket to connect to: explicit ctor arg → env → dev-spawned broker."""
        if self._sock_path:
            return self._sock_path, self._token
        env_sock = os.environ.get("GINNO_MOD_BROKER_SOCK")
        if env_sock:
            token = None
            token_path = os.environ.get("GINNO_MOD_BROKER_TOKEN")
            if token_path:
                try:
                    token = open(token_path).read().strip() or None
                except OSError:
                    token = None
            return env_sock, token
        from .bridge_utils import ensure_broker

        return await ensure_broker()

    async def _run_connection(self, reader: Any, writer: Any) -> None:
        # Dedicated reader task + AsyncExitStack so a handshake failure or a
        # dropped socket always unwinds writer + reader together (the
        # _LiveServer pattern in mcp/registry.py).
        async with AsyncExitStack() as stack:
            self._writer = writer

            async def _close_writer() -> None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 — already half-closed somewhere
                    pass

            stack.push_async_callback(_close_writer)
            reader_task = asyncio.get_running_loop().create_task(self._reader_loop(reader))
            stack.push_async_callback(self._cancel_task, reader_task)
            # hello handshake: role + token + the full mods config (design
            # §6.2). The broker only spawns runners after this.
            try:
                ready = await self.call(
                    "broker",
                    "hello",
                    {"role": "runtime", "token": self._token, "config": self.build_config()},
                    timeout=self._hello_timeout,
                )
            except Exception:
                self._registered = None
                raise
            self._absorb_ready(ready)
            self._set_status("connected")
            log.info("mods channel connected (mods=%d)", len(self.mods_state))
            await reader_task

    @staticmethod
    async def _cancel_task(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _teardown_connection(self) -> None:
        self._writer = None
        self._registered = None
        self.mods_state.clear()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ChannelNotConnected("connection lost"))
        self._pending.clear()

    def _absorb_ready(self, ready: Any) -> None:
        """Parse the hello result: mod lifecycle cache + registered events."""
        self.mods_state = {}
        events: set[str] = set()
        known = False
        if isinstance(ready, dict):
            mods = ready.get("mods")
            if isinstance(mods, list):
                known = True
                for m in mods:
                    if not isinstance(m, dict):
                        continue
                    name = str(m.get("name") or "")
                    if name:
                        self.mods_state[name] = {
                            "name": name,
                            "status": m.get("status") or "loaded",
                            **{k: v for k, v in m.items() if k not in ("name", "status", "hooks")},
                        }
                    raw_hooks = m.get("hooks")
                    if isinstance(raw_hooks, str):
                        # The broker's ready carries the validate-style describe
                        # string ("turn.start, tool.call{tool=/^Write/}") — split
                        # on commas and drop the matcher braces.
                        for part in raw_hooks.split(","):
                            name = part.split("{", 1)[0].strip()
                            if name:
                                events.add(name)
                    else:
                        for h in raw_hooks or []:
                            if isinstance(h, dict) and h.get("event"):
                                events.add(str(h["event"]))
        # No mods list at all → unknown registry (always forward); an explicit
        # (possibly empty) list → exact short-circuit set.
        self._registered = events if known else None

    def _set_status(self, status: str, detail: str = "") -> None:
        self._status = status
        self._status_detail = detail

    # ---- reader loop ----------------------------------------------------------

    async def _reader_loop(self, reader: Any) -> None:
        while True:
            line = await reader.readline()
            if not line:
                return  # EOF — broker went away; the connect loop reconnects
            try:
                frame = decode_frame(line)
            except (json.JSONDecodeError, FrameError, UnicodeDecodeError):
                log.warning("mods channel dropped malformed frame (%d bytes)", len(line))
                continue
            kind = frame.get("kind")
            if kind == "result":
                fut = self._pending.pop(frame.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(frame)
            elif kind == "call":
                asyncio.get_running_loop().create_task(self._serve_call(frame))
            elif kind == "notify":
                from ..server_shared import spawn_bg

                spawn_bg(self._handle_notify(frame))
            else:
                # event / next / catch-call: Python is an event SOURCE in P0 —
                # the broker never dispatches hook chains back at us.
                log.debug("mods channel ignoring frame kind=%s", kind)

    async def _serve_call(self, frame: dict) -> None:
        """Answer a broker-forwarded Python backend op (design §5.4)."""
        from . import ops

        frame_id = frame.get("id")
        ns = str(frame.get("ns") or "")
        method = str(frame.get("method") or "")
        try:
            value = await ops.handle(ns, method, frame.get("args") or {}, str(frame.get("session") or ""))
            reply = {"v": PROTOCOL_VERSION, "id": frame_id, "kind": "result", "ok": True, "value": value}
        except ops.OpError as e:
            reply = {"v": PROTOCOL_VERSION, "id": frame_id, "kind": "result", "ok": False, "code": e.code, "message": str(e)}
        except Exception as e:  # noqa: BLE001 — report, never crash the reader
            log.exception("mods op %s.%s failed", ns, method)
            reply = {
                "v": PROTOCOL_VERSION,
                "id": frame_id,
                "kind": "result",
                "ok": False,
                "code": "internal",
                "message": f"{type(e).__name__}: {e}",
            }
        try:
            await self._send(reply)
        except Exception:  # noqa: BLE001 — socket died mid-reply; reconnect handles it
            pass

    async def _handle_notify(self, frame: dict) -> None:
        """Fan a broker notify out to the WS layer (single frontend channel)."""
        method = str(frame.get("method") or "")
        args = frame.get("args") or {}
        session = frame.get("session") or None
        if method == "mod.status":
            # Lifecycle (loaded/error/disabled/restarting): refresh the cache
            # the API reads, then broadcast to every open settings page.
            name = str(args.get("name") or "")
            if name:
                entry = self.mods_state.setdefault(name, {"name": name})
                entry.update({k: v for k, v in args.items() if k != "name"})
            await self._broadcast(None, "mod.state.changed", args)
            return
        ws_event = _NOTIFY_TO_WS.get(method)
        if ws_event is None:
            log.debug("mods notify with unknown method=%s dropped", method)
            return
        if ws_event == "mod.bands":
            await self._broadcast(session, ws_event, args)
        else:
            await self._broadcast(None, ws_event, args)

    async def _broadcast(self, session_id: str | None, event: str, data: dict) -> None:
        from ..server_shared import _push_global_event, _push_session_event

        try:
            if session_id:
                await _push_session_event(session_id, event, data)
            else:
                await _push_global_event(event, data)
        except Exception:  # noqa: BLE001 — WS fan-out must never kill the channel
            log.exception("mods ws broadcast failed event=%s", event)

    # ---- sending ------------------------------------------------------------

    async def _send(self, frame: dict) -> None:
        writer = self._writer
        if writer is None:
            raise ChannelNotConnected("not connected")
        writer.write(encode_frame(frame))
        await writer.drain()

    async def call(self, ns: str, method: str, args: dict, *, session: str | None = None, timeout: float = 10.0) -> Any:
        """Python→broker request-response ($ management ops: config push,
        validate). Raises ChannelNotConnected / OpError-shaped failures."""
        frame_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = fut
        try:
            await self._send({
                "v": PROTOCOL_VERSION,
                "id": frame_id,
                "kind": "call",
                "ns": ns,
                "method": method,
                "args": args,
                **({"session": session} if session else {}),
            })
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(frame_id, None)
        if not isinstance(reply, dict):
            raise FrameError("non-object result frame")
        if reply.get("ok") is False:
            from .ops import OpError

            raise OpError(str(reply.get("code") or "error"), str(reply.get("message") or ""))
        return reply.get("value")

    async def notify(self, method: str, args: dict, *, session: str | None = None) -> None:
        """Fire-and-forget notify to the broker (``ui.press`` — the broker
        validates the generation and routes to the owning mod's runner). No-op
        when disconnected: a press on a dead band must never surface as a turn
        error."""
        if not self.is_connected:
            return
        await self._send({
            "v": PROTOCOL_VERSION,
            "kind": "notify",
            "method": method,
            **({"session": session} if session else {}),
            "args": args,
        })

    # ---- event dispatch (design §5.3) -----------------------------------------

    async def dispatch_event(self, session_id: str, event: str, payload: dict, deadline_ms: int = 10_000) -> dict:
        """Send one engine event and await the broker's chain-settle result.

        Per-session serial (queued), cross-session parallel. Any failure —
        not connected, timeout, error reply — resolves with the ORIGINAL
        payload (放行): mods are strictly additive to the turn path.
        """
        if not self.active_for(event):
            return payload
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        queue = self._queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[session_id] = queue
            self._workers[session_id] = asyncio.get_running_loop().create_task(
                self._drain_session(session_id, queue)
            )
        queue.put_nowait((fut, event, payload, deadline_ms))
        try:
            return await fut
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — worker already resolved failures; belt & braces
            return payload

    async def _drain_session(self, session_id: str, queue: asyncio.Queue) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return  # stop() sentinel
            fut, event, payload, deadline_ms = item
            result = await self._send_event(session_id, event, payload, deadline_ms)
            if not fut.done():
                fut.set_result(result)

    async def _send_event(self, session_id: str, event: str, payload: dict, deadline_ms: int) -> dict:
        if not self.is_connected:
            return payload
        frame_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = fut
        try:
            await self._send({
                "v": PROTOCOL_VERSION,
                "id": frame_id,
                "kind": "event",
                "event": event,
                "session": session_id,
                # Same value as id: the invocation id threads the chain state
                # machine on the broker side (§6.1); carrying both keeps the
                # pending-map correlation and the invocation audit separate.
                "invocation": f"py-{frame_id}",
                "payload": payload,
                "deadlineMs": deadline_ms,
            })
            reply = await asyncio.wait_for(fut, deadline_ms / 1000 + _DEADLINE_GRACE_S)
        except Exception:  # noqa: BLE001 — timeout / disconnect / send failure → 放行
            return payload
        finally:
            self._pending.pop(frame_id, None)
        if not isinstance(reply, dict) or reply.get("ok") is False:
            return payload
        value = reply.get("value")
        return value if isinstance(value, dict) else payload

    # ---- config (settings.json is ours; broker gets a push, design §9) ---------

    def build_config(self) -> dict:
        """The config payload the hello handshake (and every re-push) carries.
        Schema = the broker's ``parse_config`` (crates/mod-broker/src/lib.rs):
        nodePath/runnerPath are required and must be *resolved* paths, and
        mods live under ``mods.items.<name>`` with a ``dir`` each."""
        from .bridge_utils import load_mods_settings, mods_dir, resolve_node, resolve_runner, scan_installed_mods

        cfg = load_mods_settings()
        # Base items from the installed directories (drop-in convention), so a
        # mod the user cloned without touching settings still gets a dir.
        # JS-shaped only: classic {"hooks":{...}} plugins ride the
        # HookDispatcher (§10) — spawning a runner for them crash-loops.
        items: dict[str, dict] = {}
        for m in scan_installed_mods():
            if m.get("shape") not in (None, "js"):
                continue
            items[m["name"]] = {"enabled": True, "dir": m["path"], "grants": {}, "config": {}}
        for name, item in (cfg.get("items") or {}).items():
            if not isinstance(item, dict):
                continue
            base = items.get(name, {"dir": str(mods_dir() / name)})
            if item.get("dir"):
                base["dir"] = str(item["dir"])
            for key in ("enabled", "grants", "config"):
                if key in item:
                    base[key] = item[key]
            items[name] = base
        node_path = resolve_node() or cfg.get("nodePath") or ""
        runner_path = resolve_runner() or ""
        # JS mods need both binaries; push an empty path and every runner
        # spawn fails for the whole broker session (broker now fails fast,
        # but the root cause should be visible here at hello time).
        if not node_path:
            log.warning("mods config: node not resolved — runner mods unavailable (need Node ≥22.18; set mods.nodePath)")
        if not runner_path:
            log.warning(
                "mods config: runner bundle not resolved — runner mods unavailable "
                "(build packages/mod-runner: pnpm build; or install dist/mod-runner.mjs to ~/.ginno/bin)"
            )
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "allowOverrideDenyRules": bool(cfg.get("allowOverrideDenyRules", False)),
            # Resolved binaries (settings nodePath already rode resolve_node).
            "nodePath": node_path,
            "runnerPath": runner_path,
            "mods": {"items": items},
            # Budget values the broker's BudgetClock enforces (§3.2); the
            # broker's key names, defaulting to the spec numbers.
            "budgets": {
                "hookMs": 10_000,
                "promptEditMs": 50,
                "catchMs": 1000,
                "sessionEndMs": 1500,
            },
        }

    async def push_config(self) -> None:
        """Re-push config after a settings change (broker diffs → per-mod
        reload). No-op when not connected; the next hello carries it anyway."""
        if not self.is_connected:
            return
        await self.call("broker", "config", {"role": "runtime", "config": self.build_config()})

    def availability(self) -> dict:
        """Two-level probe for the settings page (external-agents 同款):
        status of the channel + what the local environment could provide."""
        import os as _os

        from .bridge_utils import resolve_broker, resolve_node

        return {
            "status": self._status,
            "detail": self._status_detail,
            "connected": self.is_connected,
            "envSocket": bool(self._sock_path or _os.environ.get("GINNO_MOD_BROKER_SOCK")),
            "brokerPath": resolve_broker(),
            "nodePath": resolve_node(),
            "mods": sorted(self.mods_state.values(), key=lambda m: m.get("name") or ""),
        }


# ---- module singleton ------------------------------------------------------------

_channel: ModChannel | None = None


def get_channel() -> ModChannel:
    global _channel
    if _channel is None:
        _channel = ModChannel()
    return _channel


def set_channel(ch: ModChannel | None) -> None:
    """Swap the singleton (tests inject one bound to a fake broker socket)."""
    global _channel
    _channel = ch


async def shutdown_channel() -> None:
    if _channel is not None:
        await _channel.stop()
