"""P2 mods contracts (claude-code-mods-design.md §5.4/§5.3/§9): model.* ops
(usage-accounted, mock provider — never a real call), the agent registry +
agent.spawn deny tap, the session.compact skip tap, and the project-level
mods dir override/scope rules. Fully offline."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from ginno_runtime.agents import mod_registry
from ginno_runtime.compaction import maybe_compact_history
from ginno_runtime.mods import bridge_utils, ops
from ginno_runtime.mods import channel as mods_channel
from ginno_runtime.mods import events as mods_events
from ginno_runtime.mods.ops import OpError
from ginno_runtime.server_shared import _SESSIONS
from ginno_runtime.testing.fake_model import ScriptedChatModel, script

pytestmark = pytest.mark.unit


class StubChannel:
    """Scripted per-event settle values (same shape as the P0/P1 stubs)."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []
        self.settle: dict[str, dict] = {}

    def ensure_started(self):
        pass

    def active_for(self, event):
        return True

    async def dispatch_event(self, session_id, event, payload, deadline_ms=10_000):
        self.events.append((session_id, event, payload))
        extra = self.settle.get(event)
        if isinstance(extra, dict):
            return {**payload, **extra}
        return payload


@pytest.fixture
def stub_channel(monkeypatch):
    stub = StubChannel()
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    return stub


def _last_usage_row(isolated_home: Path) -> dict:
    day = time.strftime("%Y-%m-%d", time.localtime())
    p = isolated_home / "usage" / f"requests-{day}.jsonl"
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert lines, "no usage row recorded"
    return json.loads(lines[-1])


# ---- model.complete / model.classify -------------------------------------------


def test_model_complete_returns_text_and_records_usage(monkeypatch, isolated_home):
    model = ScriptedChatModel(
        scripts=[
            script(
                text="hello world",
                usage={
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                },
            )
        ]
    )
    monkeypatch.setattr(ops, "_build_model", lambda args: (model, "prov-x", "m-1"))
    import asyncio

    res = asyncio.run(
        ops.handle("model", "complete", {"prompt": "say hi", "system": "be nice"}, "s1")
    )
    assert res == {"text": "hello world"}
    row = _last_usage_row(isolated_home)
    assert row["source"] == "mods"
    assert row["provider"] == "prov-x" and row["model"] == "m-1"
    assert row["input_tokens"] == 12 and row["output_tokens"] == 5
    assert row["session_id"] == "s1"


def test_model_complete_empty_prompt_is_invalid():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "complete", {"prompt": "  "}, "s1"))
    assert ei.value.code == "invalid-argument"


def test_model_complete_provider_error_is_op_error(monkeypatch):
    def _boom(args):
        raise ValueError("provider custom is disabled")

    monkeypatch.setattr(ops, "_build_model", _boom)
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "complete", {"prompt": "x"}, "s1"))
    assert ei.value.code == "provider-unavailable"
    assert "disabled" in str(ei.value)


def test_model_classify_matches_label(monkeypatch):
    model = ScriptedChatModel(scripts=[script(text="  The input looks urgent to me. ")])
    monkeypatch.setattr(ops, "_build_model", lambda args: (model, "p", "m"))
    import asyncio

    res = asyncio.run(
        ops.handle(
            "model", "classify", {"input": "server is down!!", "labels": ["urgent", "normal"]},
            "s1",
        )
    )
    assert res == {"label": "urgent"}


def test_model_classify_exact_first_line(monkeypatch):
    model = ScriptedChatModel(scripts=[script(text='"normal"')])
    monkeypatch.setattr(ops, "_build_model", lambda args: (model, "p", "m"))
    import asyncio

    res = asyncio.run(
        ops.handle(
            "model", "classify", {"input": "all good", "labels": ["urgent", "normal"]}, "s1"
        )
    )
    assert res == {"label": "normal"}


def test_model_classify_no_label_is_named_error(monkeypatch):
    model = ScriptedChatModel(scripts=[script(text="banana")])
    monkeypatch.setattr(ops, "_build_model", lambda args: (model, "p", "m"))
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(
            ops.handle(
                "model", "classify", {"input": "x", "labels": ["urgent", "normal"]}, "s1"
            )
        )
    assert ei.value.code == "no-label"


def test_model_classify_requires_labels():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "classify", {"input": "x", "labels": []}, "s1"))
    assert ei.value.code == "invalid-argument"


def test_clamp_max_tokens():
    assert ops._clamp_max_tokens({}) is None
    assert ops._clamp_max_tokens({"maxTokens": 128_000}) == 64_000
    assert ops._clamp_max_tokens({"maxTokens": 512}) == 512
    assert ops._clamp_max_tokens({"maxTokens": 0}) is None
    assert ops._clamp_max_tokens({"maxTokens": "junk"}) is None


def test_model_ns_unknown_method():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "transcribe", {}, "s1"))
    assert ei.value.code == "no-implementation"


# ---- agent.register / agent.list / agent.spawn ---------------------------------


def test_agent_register_and_list_roundtrip():
    import asyncio

    reg = asyncio.run(
        ops.handle("agent", "register", {"name": "reviewer", "description": "reviews"}, "s1",
                   mod="guard")
    )
    assert {"name": "reviewer", "mod": "guard", "description": "reviews"} in reg
    listing = asyncio.run(ops.handle("agent", "list", {}, "s1", mod="guard"))
    assert listing == mod_registry.list_agents()
    mod_registry.clear()


def test_agent_register_invalid():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(
            ops.handle("agent", "register", {"name": "bad name!"}, "s1", mod="m")
        )
    assert ei.value.code == "invalid-argument"
    with pytest.raises(OpError):
        asyncio.run(ops.handle("agent", "register", {"name": "ok"}, "s1", mod=""))
    mod_registry.clear()


async def test_agent_spawn_deny(stub_channel):
    stub_channel.settle["agent.spawn"] = {"deny": "no spawns after midnight", "mod": "guard"}
    verdict = await mods_events.dispatch_agent_spawn("s1", "general", "do a thing", origin="user")
    assert verdict == {"deny": "no spawns after midnight", "mod": "guard"}
    sid, event, payload = stub_channel.events[-1]
    assert event == "agent.spawn" and sid == "s1"
    assert payload["agent"] == "general" and payload["task"] == "do a thing"


async def test_agent_spawn_observe(stub_channel):
    verdict = await mods_events.dispatch_agent_spawn("s1", "researcher", "dig")
    assert verdict is None
    # DSH deny spellings normalize (true / {"reason": ..}).
    stub_channel.settle["agent.spawn"] = {"deny": True}
    assert (await mods_events.dispatch_agent_spawn("s1", "a", "t"))["deny"] == "denied by mod"


async def test_create_subagent_denied_by_mod(monkeypatch):
    _SESSIONS["parent"] = {
        "session_id": "parent",
        "project_slug": "p",
        "workspace": "/tmp/ws",
        "model_provider": "x",
        "model_name": "y",
    }

    async def _deny(session_id, agent, task, *, origin="", depth=0):
        return {"deny": "spawning is off", "mod": "guard"}

    monkeypatch.setattr(mods_events, "dispatch_agent_spawn", _deny)
    from ginno_runtime.subagent_scheduler import create_subagent

    res = await create_subagent("parent", "child goal")
    assert res["ok"] is False
    assert "spawning is off" in res["error"]
    assert "guard" in res["error"]
    assert "session_id" not in res  # no child was created


# ---- session.compact -------------------------------------------------------------


async def test_compact_skip(stub_channel):
    stub_channel.settle["session.compact"] = {"skip": True}
    assert await mods_events.dispatch_session_compact(
        "s1", tokens=900_000, threshold=500_000, force=False, messages=42
    )
    _, event, payload = stub_channel.events[-1]
    assert event == "session.compact"
    assert payload["reason"] == "threshold" and payload["messages"] == 42
    # Manual /compact carries its own reason; a non-skip answer lets it run.
    stub_channel.settle.pop("session.compact")
    assert await mods_events.dispatch_session_compact(
        "s1", tokens=1, threshold=1, force=True, messages=3
    ) is False


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages
        self.updated = None

    async def aget_state(self, config):
        return SimpleNamespace(next=(), values={"messages": self._messages})

    async def aupdate_state(self, config, values, as_node=None):
        self.updated = values
        return None


class _FakeModel:
    def __init__(self):
        self.invoked = 0

    async def ainvoke(self, messages, **kwargs):
        self.invoked += 1
        return script(text="a dense summary")


def _history(n=5):
    return [HumanMessage(content=f"turn {i}") for i in range(n)]


async def test_maybe_compact_history_skip_by_mod(monkeypatch):
    graph = _FakeGraph(_history())
    model = _FakeModel()
    session = {"graph": graph, "model": model, "session_id": "s1", "project_slug": "p"}

    async def _skip(session_id, **kw):
        return True

    monkeypatch.setattr(mods_events, "dispatch_session_compact", _skip)
    stats = await maybe_compact_history(session, {"configurable": {"thread_id": "s1"}}, force=True)
    assert stats is None
    assert model.invoked == 0 and graph.updated is None  # compaction never ran


async def test_maybe_compact_history_runs_without_skip(monkeypatch):
    graph = _FakeGraph(_history())
    model = _FakeModel()
    session = {"graph": graph, "model": model, "session_id": "s1", "project_slug": "p"}

    seen: dict = {}

    async def _observe(session_id, **kw):
        seen.update(kw)
        seen["session_id"] = session_id
        return False

    monkeypatch.setattr(mods_events, "dispatch_session_compact", _observe)
    stats = await maybe_compact_history(session, {"configurable": {"thread_id": "s1"}}, force=True)
    assert stats and stats["compacted_messages"] >= 1
    assert model.invoked == 1 and graph.updated is not None
    assert seen["force"] is True  # the manual path reached the tap
    assert seen["session_id"] == "s1"


# ---- project-level mods dir (design §9/§11) --------------------------------------


def _mk_mod(root: Path, name: str, *, manifest_name: str | None = None, shape: str = "js") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if manifest_name:
        (d / "plugin.json").write_text(json.dumps({"name": manifest_name, "version": "1.0"}))
    if shape == "js":
        (d / "hooks.json").write_text(json.dumps({"modules": ["mod.ts"]}))
    return d


@pytest.fixture
def project_ws(isolated_home):
    ws = isolated_home / "ws"
    (ws / ".ginno" / "mods").mkdir(parents=True, exist_ok=True)
    return ws


def test_scan_project_overrides_global(isolated_home, project_ws, monkeypatch):
    _mk_mod(isolated_home / "mods", "alpha")
    _mk_mod(isolated_home / "mods", "beta")
    _mk_mod(project_ws / ".ginno" / "mods", "alpha")

    monkeypatch.setattr(bridge_utils, "project_mods_dir", lambda: project_ws / ".ginno" / "mods")
    rows = {r["name"]: r for r in bridge_utils.scan_installed_mods()}
    assert rows["alpha"]["scope"] == "project"
    assert rows["alpha"]["path"] == str(project_ws / ".ginno" / "mods" / "alpha")
    assert rows["beta"]["scope"] == "global"
    assert len(rows) == 2  # one row per NAME — the global alpha is shadowed


def test_scan_without_project_dir(isolated_home, monkeypatch):
    _mk_mod(isolated_home / "mods", "solo")
    monkeypatch.setattr(bridge_utils, "project_mods_dir", lambda: None)
    rows = bridge_utils.scan_installed_mods()
    assert len(rows) == 1 and rows[0]["scope"] == "global"


def test_project_mods_dir_from_live_session(isolated_home):
    ws = isolated_home / "live-ws"
    _SESSIONS["s9"] = {"workspace": str(ws)}
    try:
        d = bridge_utils.project_mods_dir()
        assert d == ws / ".ginno" / "mods"
    finally:
        _SESSIONS.pop("s9", None)


def test_build_config_dir_priority(isolated_home, project_ws, monkeypatch):
    _mk_mod(isolated_home / "mods", "alpha")
    _mk_mod(project_ws / ".ginno" / "mods", "alpha")
    _mk_mod(project_ws / ".ginno" / "mods", "proj-only")
    monkeypatch.setattr(bridge_utils, "project_mods_dir", lambda: project_ws / ".ginno" / "mods")

    ch = mods_channel.ModChannel(sock_path="/nonexistent")  # never connected in this test
    items = ch.build_config()["mods"]["items"]
    # project copy wins over the global dir for the same name…
    assert items["alpha"]["dir"] == str(project_ws / ".ginno" / "mods" / "alpha")
    # …and a project-only mod is picked up with its own dir.
    assert items["proj-only"]["dir"] == str(project_ws / ".ginno" / "mods" / "proj-only")

    # An EXPLICIT settings dir (deliberate user pointing) outranks both scans.
    bridge_utils.update_mod_item("alpha", {"dir": "/custom/alpha"})
    items = ch.build_config()["mods"]["items"]
    assert items["alpha"]["dir"] == "/custom/alpha"
    # The broker item schema stays dir/enabled/grants/config only.
    assert set(items["alpha"].keys()) == {"enabled", "dir", "grants", "config"}


def test_merged_mod_list_carries_scope(isolated_home, project_ws, monkeypatch):
    _mk_mod(isolated_home / "mods", "beta")
    _mk_mod(project_ws / ".ginno" / "mods", "alpha")
    monkeypatch.setattr(bridge_utils, "project_mods_dir", lambda: project_ws / ".ginno" / "mods")
    from ginno_runtime.mods.api import _merged_mod_list

    rows = {r["name"]: r for r in _merged_mod_list()}
    assert rows["alpha"]["scope"] == "project" and rows["alpha"]["installed"] is True
    assert rows["beta"]["scope"] == "global"
