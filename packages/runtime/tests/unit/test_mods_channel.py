"""Unit tests for the mods bus Python side (claude-code-mods-design.md §5/§6).

Everything runs against a FAKE broker: a real asyncio unix server speaking the
newline-JSON frame protocol in a tmp dir — no Rust binary, no node, hermetic.
Covers: frame codec, hello/config handshake, per-session serial dispatch,
call/result correlation, timeout/disconnect 放行, the zero-mod short-circuit,
and the event payload/deny shapes of events.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ginno_runtime.mods import channel as mods_channel
from ginno_runtime.mods import events as mods_events
from ginno_runtime.mods.channel import (
    ModChannel,
    decode_frame,
    encode_frame,
)

pytestmark = pytest.mark.unit


# ---- frame codec ------------------------------------------------------------


def test_frame_codec_roundtrip():
    frame = {"v": 1, "kind": "event", "event": "turn.start", "payload": {"text": "héllo"}}
    assert decode_frame(encode_frame(frame)) == frame


def test_frame_codec_rejects_non_object():
    import pytest as _pytest

    with _pytest.raises(mods_channel.FrameError):
        decode_frame(b"[1,2,3]\n")
    import json as _json

    with _pytest.raises(_json.JSONDecodeError):
        decode_frame(b"not json\n")


# ---- fake broker -------------------------------------------------------------

# Sentinel: an on_event returning this answers NOTHING (deadline timeout).
NO_REPLY = object()


class FakeBroker:
    """A scriptable stand-in for ginno-mod-broker on a real unix socket.

    ``on_event`` is an async callable(frame) → reply value (or NO_REPLY for no
    reply, i.e. a deadline timeout). The hello always answers with
    ``ready_value`` (default: one loaded mod registered on turn.start).
    """

    def __init__(self, sock_path: str, ready_value: dict | None = None, on_event=None):
        self.sock_path = sock_path
        self.ready_value = ready_value if ready_value is not None else {
            "mods": [
                {"name": "token-weather", "status": "loaded", "hooks": [{"event": "turn.start"}]}
            ]
        }
        self.on_event = on_event
        self.received: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    async def start(self) -> FakeBroker:
        self._server = await asyncio.start_unix_server(self._serve_client, path=self.sock_path)
        return self

    async def stop(self) -> None:
        for _r, w in self._clients:
            w.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._clients.append((reader, writer))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.received.append(frame)
                # One task per frame: the broker serves frames concurrently
                # (a slow handler must not delay the next frame's read).
                asyncio.get_running_loop().create_task(self._handle(frame, writer))
        except (ConnectionError, RuntimeError):
            pass

    async def _handle(self, frame: dict, writer: asyncio.StreamWriter) -> None:
        reply: dict | None = None
        if frame.get("kind") == "call" and frame.get("method") == "hello":
            reply = {
                "v": 1,
                "id": frame.get("id"),
                "kind": "result",
                "ok": True,
                "value": self.ready_value,
            }
        elif frame.get("kind") == "call":
            reply = {
                "v": 1,
                "id": frame.get("id"),
                "kind": "result",
                "ok": True,
                "value": {"echo": frame.get("method")},
            }
        elif frame.get("kind") == "event" and self.on_event is not None:
            value = await self.on_event(frame)
            if value is not NO_REPLY:
                reply = {
                    "v": 1,
                    "id": frame.get("id"),
                    "kind": "result",
                    "ok": True,
                    "value": value,
                }
        if reply is not None:
            writer.write(encode_frame(reply))
            await writer.drain()


@pytest.fixture
async def connected(monkeypatch, tmp_path):
    """A ModChannel connected to a fresh FakeBroker. Yields (channel, broker)."""
    # Replace ensure_started with a gate-free version: the tests install their
    # own broker socket, so the enabled/no-mods discovery gates only add noise.
    def _ensure_started(self: ModChannel) -> None:
        if self._closing or (self._task and not self._task.done()):
            return
        loop = asyncio.get_running_loop()
        self._status = "connecting"
        self._task = loop.create_task(self._connect_loop())

    monkeypatch.setattr(ModChannel, "ensure_started", _ensure_started)

    # AF_UNIX paths cap at ~104 chars on macOS and pytest tmp_path paths are
    # far deeper — put the test socket in the short /tmp dir instead.
    import shutil
    import tempfile

    sock_dir = tempfile.mkdtemp(prefix="mods-test-")
    sock_path = str(Path(sock_dir) / "b.sock")
    broker = await FakeBroker(sock_path).start()
    ch = ModChannel(sock_path=sock_path)
    ch.ensure_started()
    for _ in range(200):
        if ch.is_connected:
            break
        await asyncio.sleep(0.01)
    assert ch.is_connected, f"channel did not connect (status={ch.status})"
    yield ch, broker
    await ch.stop()
    await broker.stop()
    shutil.rmtree(sock_dir, ignore_errors=True)


# ---- handshake / registry ----------------------------------------------------


async def test_hello_absorbs_ready(connected):
    ch, broker = connected
    assert "token-weather" in ch.mods_state
    assert ch._registered == {"turn.start"}


async def test_event_unregistered_short_circuits(connected):
    ch, broker = connected
    hello_plus = len(broker.received)
    out = await ch.dispatch_event("s1", "tool.call", {"tool": "bash"}, deadline_ms=200)
    assert out == {"tool": "bash"}  # original payload, untouched
    assert len(broker.received) == hello_plus  # nothing hit the wire


async def test_event_registered_dispatched_and_rewritten(connected):
    ch, broker = connected

    async def on_event(frame):
        assert frame["event"] == "turn.start"
        assert frame["session"] == "s1"
        assert frame["invocation"].startswith("py-")
        assert frame["deadlineMs"] == 10_000
        return {"marker": "from-mod"}

    broker.on_event = on_event
    out = await ch.dispatch_event("s1", "turn.start", {"turnId": "t1"})
    assert out == {"marker": "from-mod"}


async def test_event_timeout_returns_original_payload(connected, monkeypatch):
    ch, broker = connected
    monkeypatch.setattr(mods_channel, "_DEADLINE_GRACE_S", 0.05)

    async def on_event(frame):
        await asyncio.sleep(1.0)  # far past the deadline
        return {"late": True}

    broker.on_event = on_event
    out = await ch.dispatch_event("s1", "turn.start", {"turnId": "t1"}, deadline_ms=100)
    assert out == {"turnId": "t1"}  # 放行: a hanging mod can never stall a turn


async def test_disconnect_resolves_with_original_payload(connected):
    ch, broker = connected
    broker.on_event = None  # no reply; we kill the socket instead

    async def kill_soon():
        await asyncio.sleep(0.05)
        await broker.stop()

    task = asyncio.get_running_loop().create_task(kill_soon())
    out = await ch.dispatch_event("s1", "turn.start", {"turnId": "t1"}, deadline_ms=5_000)
    await task
    assert out == {"turnId": "t1"}


async def test_per_session_serial_cross_session_parallel(connected):
    ch, broker = connected
    order: list[str] = []
    done: list[str] = []

    async def on_event(frame):
        tag = f"{frame['session']}:{frame['payload']['n']}"
        order.append(tag)
        await asyncio.sleep(0.05 if frame["session"] == "A" else 0.0)
        return frame["payload"]

    broker.on_event = on_event
    ch._registered = {"turn.start"}  # all four events pass the gate

    async def send(sid, n):
        out = await ch.dispatch_event(sid, "turn.start", {"n": n})
        done.append(f"{sid}:{out['n']}")

    await asyncio.gather(send("A", 1), send("A", 2), send("B", 1))
    # Same session: FIFO on the wire and on completion.
    assert [t for t in order if t.startswith("A:")] == ["A:1", "A:2"]
    assert [t for t in done if t.startswith("A:")] == ["A:1", "A:2"]
    # Cross-session parallelism: B's zero-delay reply lands while A1's 50ms
    # sleep is still running → B completes first despite being sent second.
    assert done[0] == "B:1"


async def test_call_result_correlation(connected):
    ch, broker = connected
    r1, r2 = await asyncio.gather(
        ch.call("broker", "config", {"a": 1}),
        ch.call("broker", "validate", {"b": 2}),
    )
    assert r1 == {"echo": "config"}
    assert r2 == {"echo": "validate"}
    assert not ch._pending  # nothing leaks in the pending map


async def test_config_push_carries_broker_schema(connected):
    ch, broker = connected
    await ch.push_config()
    cfg_frames = [f for f in broker.received if f.get("method") == "config"]
    assert cfg_frames, "config re-push never hit the wire"
    cfg = cfg_frames[-1]["args"]["config"]
    # The broker's parse_config contract (crates/mod-broker/src/lib.rs):
    # resolved binaries, mods under mods.items with a dir each, budgets
    # under the broker's key names.
    assert cfg["nodePath"] and cfg["runnerPath"]
    items = cfg["mods"]["items"]
    assert isinstance(items, dict) and all("dir" in v for v in items.values())
    assert cfg["budgets"] == {
        "hookMs": 10_000,
        "promptEditMs": 50,
        "catchMs": 1000,
        "sessionEndMs": 1500,
    }


async def test_zero_mod_gate_never_starts(monkeypatch, tmp_path):
    """No installed mods + no settings items → the bus stays disabled and no
    connection is attempted (the zero-overhead contract, §5.3)."""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))  # empty mods dir
    monkeypatch.delenv("GINNO_MOD_BROKER_SOCK", raising=False)
    ch = ModChannel(sock_path=str(tmp_path / "unused.sock"))
    ch.ensure_started()
    assert ch.status == "disabled"
    assert ch._task is None


# ---- events.py payload / deny shapes -------------------------------------------


@pytest.fixture
def stub_channel(monkeypatch):
    """A channel stub wired through set_channel so events.py funnels hit it."""

    class Stub:
        def __init__(self):
            self.events: list[tuple[str, str, dict]] = []
            self.replies: dict[str, dict] = {}

        def ensure_started(self):
            pass

        def active_for(self, event):
            return True

        async def dispatch_event(self, session_id, event, payload, deadline_ms=10_000):
            self.events.append((session_id, event, payload))
            return self.replies.get(event, payload)

    stub = Stub()
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    monkeypatch.setattr(mods_events, "get_channel", lambda: stub, raising=False)
    # events.py imports get_channel lazily inside maybe_dispatch from .channel
    yield stub


async def test_tool_call_deny_shape(stub_channel):
    stub_channel.replies["tool.call"] = {"tool": "bash", "deny": {"reason": "blast radius"}}
    assert await mods_events.dispatch_tool_call("s1", "bash", {"cmd": "rm"}) == {
        "deny": "blast radius"
    }


async def test_tool_call_deny_nested_answer(stub_channel):
    stub_channel.replies["tool.call"] = {"answer": {"deny": True}}
    assert await mods_events.dispatch_tool_call("s1", "bash", {}) == {
        "deny": "denied by mod"
    }


async def test_tool_call_no_deny_passes(stub_channel):
    stub_channel.replies["tool.call"] = {"tool": "bash"}
    assert await mods_events.dispatch_tool_call("s1", "bash", {}) is None


async def test_prompt_submit_payload_shape(stub_channel):
    await mods_events.dispatch_prompt_submit("s1", "hello")
    sid, event, payload = stub_channel.events[-1]
    assert event == "prompt.submit"
    assert payload == {"sessionId": "s1", "text": "hello", "origin": {"kind": "composer"}}


async def test_turn_complete_payload_shape(stub_channel, monkeypatch):
    from ginno_runtime import server_shared

    captured = {}

    def fake_spawn(coro):
        captured["coro"] = coro

        class _T:
            def done(self):
                return False

        return _T()

    monkeypatch.setattr(server_shared, "spawn_bg", fake_spawn)
    server_shared._USAGE_BY_SESSION["s1"] = {"input": 5}
    mods_events.spawn_turn_complete(
        "s1", "t1", answer="done", duration_ms=1234, is_aborted=False, reason="answer"
    )
    await captured["coro"]
    sid, event, payload = stub_channel.events[-1]
    assert event == "turn.complete"
    assert payload["answer"] == "done"
    assert payload["durationMs"] == 1234
    assert payload["reason"] == "answer"
    assert payload["isAborted"] is False
    assert payload["usage"] == {"input": 5}


async def test_classic_hooks_run_before_mods(stub_channel, monkeypatch):
    """Ordering contract (§10): the classic dispatcher answers first, the mods
    broker second — verified by the sequence of observed calls."""
    order: list[str] = []

    class StubHooks:
        async def dispatch(self, event, matcher=None):
            order.append(f"classic:{event.name}")
            return []

    from ginno_runtime import server_shared

    monkeypatch.setattr(server_shared, "_hooks", StubHooks())

    real_dispatch = stub_channel.dispatch_event

    async def spy(session_id, event, payload, deadline_ms=10_000):
        order.append(f"mods:{event}")
        return await real_dispatch(session_id, event, payload, deadline_ms)

    monkeypatch.setattr(stub_channel, "dispatch_event", spy)
    await mods_events.dispatch_session_start("s1", {"workspace": "/w"})
    await mods_events.dispatch_session_end("s1")
    assert order == [
        "classic:SessionStart",
        "mods:session.start",
        "classic:SessionEnd",
        "mods:session.end",
    ]


async def test_classic_prompt_rewrite_and_block(stub_channel, monkeypatch):
    from ginno_runtime import server_shared

    class StubHooks:
        def __init__(self, results):
            self._results = results

        async def dispatch(self, event, matcher=None):
            return self._results

    from ginno_runtime.hooks.dispatcher import HookResult

    monkeypatch.setattr(
        server_shared, "_hooks", StubHooks([HookResult(rewrite="rewritten")])
    )
    r = await mods_events.dispatch_prompt_submit("s1", "original")
    assert (r.text, r.blocked) == ("rewritten", False)

    monkeypatch.setattr(server_shared, "_hooks", StubHooks([HookResult(block=True, reason="no")]))
    r = await mods_events.dispatch_prompt_submit("s1", "original")
    assert r.blocked is True and r.source == "classic"
