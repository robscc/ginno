"""Per-mod compatibility counters (design §7.5 持续项).

The channel tracks, in memory: events × fired (per dispatch, per mod whose
registration covers the event), hook failures relayed via ``mod.report``,
unimplemented `$` mentions from the runner's report lines, and the
Python-backed op slice × ok. GET /api/mods rows carry the summary as
``compat``. A disconnect clears everything.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from ginno_runtime.mods.channel import (
    ModChannel,
    _covers_event,
    _hook_event_names,
)
# The FakeBroker + ``connected`` fixture (channel ↔ fake broker on a real unix
# socket) live in the channel spec; the echo variant below re-uses the broker.
from test_mods_channel import FakeBroker

pytestmark = pytest.mark.unit


@pytest.fixture
async def connected(monkeypatch):
    """A channel connected to a FakeBroker that settles every event with its
    own payload (no deadline wait — the shared fixture's silent broker makes
    each dispatch run out its 10 s budget)."""
    def _ensure_started(self: ModChannel) -> None:
        if self._closing or (self._task and not self._task.done()):
            return
        import asyncio

        self._status = "connecting"
        self._task = asyncio.get_running_loop().create_task(self._connect_loop())

    monkeypatch.setattr(ModChannel, "ensure_started", _ensure_started)

    sock_dir = tempfile.mkdtemp(prefix="mods-compat-")
    sock_path = str(Path(sock_dir) / "b.sock")

    async def echo(frame):
        return frame.get("payload")

    broker = await FakeBroker(
        sock_path,
        ready_value={
            "mods": [{"name": "token-weather", "status": "loaded", "hooks": [{"event": "turn.start"}]}]
        },
        on_event=echo,
    ).start()
    ch = ModChannel(sock_path=sock_path)
    ch.ensure_started()
    for _ in range(200):
        if ch.is_connected:
            break
        import asyncio

        await asyncio.sleep(0.01)
    assert ch.is_connected, f"channel did not connect (status={ch.status})"
    yield ch, broker
    await ch.stop()
    await broker.stop()
    shutil.rmtree(sock_dir, ignore_errors=True)


# ---- parsing helpers ----------------------------------------------------------


def test_hook_event_names_from_describe_string():
    assert _hook_event_names("turn.start, tool.call{tool=/^Write/}, session.*") == [
        "turn.start", "tool.call", "session.*",
    ]


def test_hook_event_names_from_list():
    assert _hook_event_names([{"event": "turn.start"}, {"event": "ui.render", "matcher": {}}, "junk"]) == [
        "turn.start", "ui.render",
    ]


def test_covers_event_glob():
    assert _covers_event(["turn.start"], "turn.start")
    assert _covers_event(["session.*"], "session.end")
    assert _covers_event(["*"], "anything.at.all")
    assert not _covers_event(["turn.start"], "turn.complete")
    assert not _covers_event(["session.*"], "turn.start")


# ---- hello-time init + dispatch counting (against the FakeBroker) --------------


async def test_dispatch_counts_fired(connected):
    ch, _broker = connected
    # The default ready: token-weather registered on turn.start (list form).
    await ch.dispatch_event("s1", "turn.start", {"sessionId": "s1"})
    compat = ch.compat["token-weather"]
    assert compat["events"]["turn.start"]["fired"] == 1
    assert compat["events"]["turn.start"]["registered"] is True
    # An event nobody registered leaves no trace.
    await ch.dispatch_event("s1", "session.end", {"sessionId": "s1"})
    assert "session.end" not in compat["events"]


async def test_describe_string_hooks_also_count(connected, monkeypatch):
    ch, _broker = connected
    # Re-run absorb with the describe-string form the broker actually sends.
    ch._absorb_ready({"mods": [{"name": "m1", "status": "loaded", "hooks": "turn.start, ui.*"}]})
    await ch.dispatch_event("s1", "turn.start", {})
    await ch.dispatch_event("s1", "ui.press", {})
    assert ch.compat["m1"]["events"]["turn.start"]["fired"] == 1
    assert ch.compat["m1"]["events"]["ui.press"]["fired"] == 1
    # mods_state carries the normalized hook list for the API rows.
    assert ch.mods_state["m1"]["hooks"] == ["turn.start", "ui.*"]


# ---- report counting ------------------------------------------------------------


async def test_report_counts_hook_failure_and_unimplemented(connected):
    ch, _broker = connected
    await ch._handle_notify({
        "kind": "notify",
        "method": "mod.report",
        "args": {"line": "token-weather: turn.complete hook skipped: TypeError: boom"},
    })
    await ch._handle_notify({
        "kind": "notify",
        "method": "mod.report",
        "args": {"line": "token-weather: $.mcp.call failed: Error: no implementation for mcp.call"},
    })
    compat = ch.compat["token-weather"]
    assert compat["events"]["turn.complete"]["failed"] == 1
    assert compat["unimplemented"] == {"mcp.call": 1}


async def test_report_ignores_unknown_mods(connected):
    ch, _broker = connected
    await ch._handle_notify({
        "kind": "notify",
        "method": "mod.report",
        "args": {"line": "ghost-mod: turn.start hook skipped: Error: x"},
    })
    assert "ghost-mod" not in ch.compat


# ---- op counting ----------------------------------------------------------------


async def test_serve_call_counts_python_backed_ops(connected):
    ch, _broker = connected
    # An op no backend serves → ok=False; the counter still records the call.
    await ch._serve_call({"id": 99, "kind": "call", "ns": "bogus", "method": "x", "mod": "token-weather"})
    assert ch.compat["token-weather"]["ops"]["bogus.x"] == {"called": 1, "ok": 0}


def test_count_op_ok_path():
    ch = ModChannel(sock_path="/unused.sock")
    ch._count_op("m", "session.usage", True)
    ch._count_op("m", "session.usage", True)
    ch._count_op("m", "session.usage", False)
    assert ch.compat["m"]["ops"]["session.usage"] == {"called": 3, "ok": 2}


# ---- teardown + API shape --------------------------------------------------------


async def test_teardown_clears_compat(connected):
    ch, _broker = connected
    await ch.dispatch_event("s1", "turn.start", {})
    assert ch.compat
    ch._teardown_connection()
    assert ch.compat == {}


async def test_compat_summary_is_a_copy(connected):
    ch, _broker = connected
    await ch.dispatch_event("s1", "turn.start", {})
    summary = ch.compat_summary()
    summary["token-weather"]["events"]["turn.start"]["fired"] = 999
    assert ch.compat["token-weather"]["events"]["turn.start"]["fired"] == 1
