"""P2 mods contracts (claude-code-mods-design.md §5.4/§5.3/§9): model.* ops
(usage-accounted, mock provider — never a real call), the agent registry +
agent.spawn deny tap, the session.compact skip tap, and the project-level
mods dir override/scope rules. Fully offline."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from ginno_runtime import jsonx as json
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


# ---- model.fork / prompt.fill / prompt.suggest（2026-10-20，next-steps 移植）----

def _seed_session_history(session_id: str, *, slug: str = "default") -> None:
    """给 _rebuild_history 造可读的磁盘 checkpoint（FileCheckpointer full 模式）。"""
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage

    from ginno_runtime.checkpointer import FileCheckpointer
    from ginno_runtime.server_shared import _SESSIONS

    _SESSIONS[session_id] = {
        "id": session_id,
        "project_slug": slug,
        "agent_id": "dev",
        "model_provider": "custom",
        "model_name": "m",
    }
    ckpt = FileCheckpointer(slug)
    asyncio.run(
        ckpt.aput(
            {"configurable": {"thread_id": session_id}},
            {
                "v": 1,
                "id": "c1",
                "ts": "2026-10-20",
                "channel_values": {
                    "messages": [HumanMessage(content="hi"), AIMessage(content="hello")],
                },
                "channel_versions": {},
                "versions_seen": {},
            },
            {"source": "input", "step": 0, "writes": {}},
            {},
        )
    )


def test_model_fork_answers_with_session_context(monkeypatch, isolated_home):
    import asyncio

    _seed_session_history("s-fork")
    seen: dict = {}

    class _SpyModel(ScriptedChatModel):
        async def ainvoke(self, messages, *a, **k):
            seen["messages"] = messages
            return await super().ainvoke(messages, *a, **k)

    model = _SpyModel(scripts=[script(text='[{"label":"l","prompt":"p"}]')])
    monkeypatch.setattr(ops, "_build_model", lambda args: (model, "prov-x", "m-1"))
    res = asyncio.run(ops.handle("model", "fork", {"prompt": "predict next"}, "s-fork"))
    assert res["isAnswered"] is True
    assert res["sessionId"] == "s-fork"
    assert res["text"] == '[{"label":"l","prompt":"p"}]'  # scripted reply 原样返回
    # 上下文 = checkpoint 历史 + fork prompt（共享上下文，不是裸 complete）
    texts = [getattr(m, "content", "") for m in seen["messages"]]
    assert texts == ["hi", "hello", "predict next"]
    row = _last_usage_row(isolated_home)
    assert row["source"] == "mods" and row["session_id"] == "s-fork"


def test_model_fork_requires_prompt_and_session():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "fork", {"prompt": " "}, "s1"))
    assert ei.value.code == "invalid-argument"
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("model", "fork", {"prompt": "p"}, ""))
    assert ei.value.code == "not-found"


def test_prompt_fill_and_suggest_push_session_event(monkeypatch):
    import asyncio

    pushed: list[tuple[str, str, dict]] = []
    from ginno_runtime import server_shared

    async def fake_push(session_id, event, data, turn_id=None):
        pushed.append((session_id, event, data))

    monkeypatch.setattr(server_shared, "_push_session_event", fake_push)
    res = asyncio.run(ops.handle("prompt", "fill", {"text": "run the tests"}, "s1"))
    assert res == {"isFilled": True}
    res = asyncio.run(ops.handle("prompt", "suggest", {"text": "fix it"}, "s1"))
    assert res == {"ok": True}
    assert pushed == [
        ("s1", "mod.prompt.fill", {"text": "run the tests"}),
        ("s1", "mod.prompt.suggest", {"text": "fix it"}),
    ]


def test_prompt_ops_reject_empty_and_missing_session():
    import asyncio

    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("prompt", "fill", {"text": ""}, "s1"))
    assert ei.value.code == "invalid-argument"
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("prompt", "suggest", {"text": "x"}, ""))
    assert ei.value.code == "not-found"
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("prompt", "nope", {"text": "x"}, "s1"))
    assert ei.value.code == "no-implementation"


def test_command_list_merges_skills(monkeypatch, isolated_home):
    """known 集合必须含 skills——next-steps 靠它放行 "/skill args" 建议。"""
    import asyncio

    from ginno_runtime.commands import mod_commands

    mod_commands.register("modcmd", "some-mod", "a mod command")
    monkeypatch.setattr(
        "ginno_runtime.skills.SkillLoader.load",
        lambda self: [SimpleNamespace(name="wiki", description="compile wiki", trigger=None, allowed_tools=None, builtin=False)],
    )
    res = asyncio.run(ops.handle("command", "list", {}, "s1"))
    names = {c["name"]: c["source"] for c in res["commands"]}
    assert names.get("modcmd") == "mod"
    assert names.get("wiki") == "skill"


# ---- P1/P2 op 族补全（2026-10-21：command.run / tool.* / mcp.call / turn.abort /
# ---- agent.spawn / prompt.read）-------------------------------------------------

def test_command_run_builtin_and_mod_paths(monkeypatch):
    import asyncio

    # builtin: /help 族里挑一个同步 handler 存在的命令(不稳定——直接验证
    # 不存在的命令是命名错误,存在性由 resolver 自己的测试覆盖)。
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("command", "run", {"command": "definitely-not-a-cmd"}, "s1"))
    assert ei.value.code == "not-found"
    # mod 命令:注册后回发 command.run 事件(stub channel settle)
    from ginno_runtime.commands import mod_commands

    mod_commands.register("mycmd", "some-mod", "d")
    stub = StubChannel()
    stub.settle["command.run"] = {"reply": "mod says hi"}
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    res = asyncio.run(ops.handle("command", "run", {"command": "/mycmd", "args": "x"}, "s1"))
    assert res == {"output": "mod says hi"}


def test_tool_register_list_and_ownership():
    import asyncio

    res = asyncio.run(
        ops.handle("tool", "register", {"name": "lookup", "description": "d", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}, "s1", "some-mod")
    )
    assert res["fullName"] == "mcp__some-mod__lookup"
    listed = asyncio.run(ops.handle("tool", "list", {}, "s1", "other-mod"))
    assert [t["fullName"] for t in listed["tools"]] == ["mcp__some-mod__lookup"]
    # 同名被别的 mod 抢注 → invalid-argument
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("tool", "register", {"name": "lookup"}, "s1", "other-mod"))
    assert ei.value.code == "invalid-argument"


def test_tool_call_routes_to_owning_mod(monkeypatch):
    import asyncio

    asyncio.run(ops.handle("tool", "register", {"name": "echo"}, "s1", "some-mod"))
    stub = StubChannel()
    stub.settle["tool.call"] = {"output": "echoed"}
    monkeypatch.setattr(mods_channel, "get_channel", lambda: stub)
    res = asyncio.run(
        ops.handle("tool", "call", {"tool": "mcp__some-mod__echo", "q": "hi"}, "s1", "other-mod")
    )
    assert res == {"output": "echoed"}


def test_mod_tools_model_side_wrapper():
    """注册表 → StructuredTool 全链(模型侧接线)。"""
    import asyncio

    from ginno_runtime.tools import mod_tools

    mod_tools._REGISTRY.clear()  # 前序用例的注册不能渗进来
    try:
        asyncio.run(ops.handle("tool", "register", {"name": "probe"}, "s1", "some-mod"))
        tools = mod_tools.build_mod_tools("s1")
        assert [t.name for t in tools] == ["mcp__some-mod__probe"]
        assert tools[0].coroutine is not None
    finally:
        mod_tools._REGISTRY.clear()


def test_mod_tools_skipped_for_scheduled_sessions(isolated_home):
    """定时任务影子会话（type=scheduled）不向模型暴露 mcp__<mod>__* 工具
    （2026-10-28：headless 任务不加载 Claude Mods）。"""
    from ginno_runtime.tools import mod_tools

    mod_tools._REGISTRY.clear()
    try:
        mod_tools.register_tool("some-mod", "probe", "desc")
        _SESSIONS["sched-mod-tools"] = {"type": "scheduled"}
        _SESSIONS["normal-mod-tools"] = {"type": None}
        assert mod_tools.build_mod_tools("sched-mod-tools") == []
        assert [t.name for t in mod_tools.build_mod_tools("normal-mod-tools")] == [
            "mcp__some-mod__probe"
        ]
    finally:
        mod_tools._REGISTRY.clear()
        _SESSIONS.pop("sched-mod-tools", None)
        _SESSIONS.pop("normal-mod-tools", None)


def test_turn_abort(monkeypatch):
    import asyncio

    stopped: list[str] = []

    async def fake_stop(sid):
        stopped.append(sid)

    monkeypatch.setattr("ginno_runtime.api.sessions.stop_session", fake_stop)
    res = asyncio.run(ops.handle("turn", "abort", {}, "s1"))
    assert res == {"stopped": True} and stopped == ["s1"]


def test_mcp_call_requires_live_server(monkeypatch):
    import asyncio

    class _Reg:
        def status(self, include_tools=False):
            return [{"name": "srv", "connected": True, "tools": [{"name": "t1"}]}]

    import ginno_runtime.server_shared as shared

    monkeypatch.setattr(shared, "_mcp", _Reg())
    # _resolve_live 找不到 live session → not-found(不炸)
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("mcp", "call", {"server": "srv", "tool": "t1"}, "s1"))
    assert ei.value.code == "not-found"
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("mcp", "call", {"server": "nope", "tool": "x"}, "s1"))
    assert ei.value.code == "not-found"


def test_agent_spawn_op_routes_to_scheduler(monkeypatch):
    import asyncio

    from ginno_runtime.subagent_scheduler import spawn as spawn_mod

    async def fake_create(**kw):
        return {"ok": bool(kw.get("goal")), "session_id": "child-1", "status": "running", "error": "" if kw.get("goal") else "goal must not be empty"}

    monkeypatch.setattr(spawn_mod, "create_subagent", fake_create)
    # create_subagent 是包内 re-export;op 经 ..subagent_scheduler 取——patch 包属性
    import ginno_runtime.subagent_scheduler as ss

    monkeypatch.setattr(ss, "create_subagent", fake_create)
    res = asyncio.run(ops.handle("agent", "spawn", {"goal": "do it"}, "s1", "some-mod"))
    assert res == {"sessionId": "child-1", "status": "running"}
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("agent", "spawn", {"goal": ""}, "s1", "some-mod"))
    assert ei.value.code == "spawn-failed"


def test_prompt_read_roundtrip(monkeypatch):
    """真实时序:op 推事件后,前端应答(resolve_prompt_read)唤醒等待的 future。"""
    import asyncio

    from ginno_runtime import server_shared

    async def fake_push(session_id, event, data, turn_id=None):
        # 前端收到 mod.prompt.read → 立即回填草稿(模拟 ChatStream 分支)
        if event == "mod.prompt.read":
            ops.resolve_prompt_read(session_id, "draft text")

    monkeypatch.setattr(server_shared, "_push_session_event", fake_push)
    res = asyncio.run(ops.handle("prompt", "read", {}, "s1"))
    assert res == {"text": "draft text"}


# ---- prompt.compose / prompt.section(2026-10-23)-------------------------------

def test_prompt_compose_and_section_lifecycle(monkeypatch):
    import asyncio

    pushed: list[tuple[str, str]] = []
    from ginno_runtime import server_shared

    async def fake_push(session_id, event, data, turn_id=None):
        if event == "mod.prompt.fill":
            pushed.append((session_id, data.get("text") or ""))

    monkeypatch.setattr(server_shared, "_push_session_event", fake_push)
    ops._PROMPT_SECTIONS.pop("s-cs", None)

    # compose:整体重置 + 合成文本推进 composer
    r = asyncio.run(ops.handle("prompt", "compose", {
        "sections": [{"id": "head", "text": "TITLE"}, {"id": "body", "text": "DETAIL"}],
    }, "s-cs"))
    assert r == {"text": "TITLE\n\nDETAIL"}
    assert pushed[-1] == ("s-cs", "TITLE\n\nDETAIL")

    # get / set / settle
    r = asyncio.run(ops.handle("prompt", "section", {"op": "get", "id": "head"}, "s-cs"))
    assert r == {"text": "TITLE", "settled": False}
    r = asyncio.run(ops.handle("prompt", "section", {"op": "set", "id": "body", "text": "V2"}, "s-cs"))
    assert r["composed"] == "TITLE\n\nV2"
    assert pushed[-1] == ("s-cs", "TITLE\n\nV2")
    r = asyncio.run(ops.handle("prompt", "section", {"op": "settle", "id": "head"}, "s-cs"))
    assert r == {"text": "TITLE", "settled": True}
    # 已冻结段:set 拒绝(settled 错误码)
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("prompt", "section", {"op": "set", "id": "head", "text": "X"}, "s-cs"))
    assert ei.value.code == "settled"
    # 非法参数
    with pytest.raises(OpError) as ei:
        asyncio.run(ops.handle("prompt", "section", {"op": "bogus", "id": "x"}, "s-cs"))
    assert ei.value.code == "invalid-argument"
