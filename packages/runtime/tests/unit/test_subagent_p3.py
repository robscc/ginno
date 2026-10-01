"""Unit tests for the subagent P3 runtime surface (shared contract items 1-5).

* type registry wiring (contract 1): agent_type hit → persona body prepended
  to the brief + toolset tightened by the type's fnmatch allow list + meta
  records agent_type; unknown type → [error] + the available list; no type →
  P2 behavior byte-for-byte (standard persona, no restriction);
* fork (contract 2): the child's initial history = freshly-id copies of the
  parent checkpoint messages + the standalone brief at the top; meta.subagent
  mode = "fork"; a fork child refuses fork=true; depth rules unchanged;
* concurrency setting (contract 3): subagent.max_concurrent read dynamically
  (settings write takes effect), GINNO_SUBAGENT_MAX overrides, clamped 1-16,
  and the cap replaces the P1 module constant in spawn + plan validation;
* soft warnings (contract 4): 80%-of-cap concurrency notice and the 500k
  cumulative subagent usage notice, each exactly once per session, Chinese
  copy with the current numbers;
* P2 leftovers (contract 5): the decompose command family runs OFF the
  receive loop (run_background_command) and cancel broadcasts
  ``subagent.plan.cancelled``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime import paths, server_shared
from ginno_runtime import subagent_plan as plan_mod
from ginno_runtime import subagent_scheduler as sched
from ginno_runtime import subagent_types as st_mod
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.commands.registry import BuiltinCommand
from ginno_runtime.session_meta import _find_meta, _session_meta_upsert

pytestmark = pytest.mark.unit

SLUG = "default"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_state():
    regs = (
        server_shared._SESSION_CHILDREN,
        server_shared._STEER_STASH,
        server_shared._RUNNING_TURNS,
        server_shared._TURN_STOP,
        server_shared._TURN_TASKS,
        # Loop-bound locks: a finalize that reached the wake path binds one
        # per session id; leaving them behind would poison the scheduler
        # tests' fixed ids on the next event loop.
        server_shared._TURN_LOCKS,
        sched._FINALIZING,
        sched._WARNED_CONCURRENCY,
        sched._WARNED_TOKENS,
        plan_mod._PENDING_PLANS,
    )
    for r in regs:
        r.clear()
    yield
    for r in regs:
        r.clear()


@pytest.fixture(autouse=True)
def _fresh_spawn_lock():
    sched._SPAWN_LOCK = asyncio.Lock()
    yield


@pytest.fixture(autouse=True)
def _no_type_files():
    """The real ~/.ginno is isolated, but keep the registry deterministic
    anyway (a leftover subagents dir in a shared temp home would leak)."""
    st_mod._CACHE = None
    st_mod._DIR_STATE = None
    yield
    st_mod._CACHE = None
    st_mod._DIR_STATE = None


def _put_meta(meta: dict) -> None:
    paths.project_sessions_dir(SLUG).mkdir(parents=True, exist_ok=True)
    _session_meta_upsert(SLUG, meta)


def _main_meta(sid: str) -> dict:
    return {"id": sid, "title": f"t-{sid}", "created": time.time(), "updated": time.time()}


def _main_session(sid: str) -> dict:
    return {
        "session_id": sid,
        "project_slug": SLUG,
        "workspace": "/tmp",
        "agent_id": "dev",
        "model_provider": "custom",
        "model_name": None,
        "type": None,
        "context_folders": [],
        "primary_folder": None,
    }


def _sub_meta(
    sid: str, parent: str | None = None, depth: int = 0, status: str = "running",
    mode: str | None = None,
) -> dict:
    m: dict = {"id": sid, "title": f"t-{sid}", "created": time.time(), "updated": time.time()}
    if parent is not None:
        sa: dict = {
            "goal": "调研 OAuth 库", "constraints": "", "acceptance": "",
            "origin": "agent", "status": status, "result_summary": "",
        }
        if mode:
            sa["mode"] = mode
        m.update({
            "type": "subagent", "parent_session_id": parent,
            "depth": depth, "subagent": sa,
        })
    return m


def _write_type(name: str, body: str = "你是只读调研员。", **fm) -> None:
    meta = {"name": name, "description": "只读调研类型", **fm}
    d = st_mod.subagents_dir()
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---"] + [f"{k}: {v}" for k, v in meta.items()] + ["---", "", body, ""]
    (d / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")
    st_mod._CACHE = None
    st_mod._DIR_STATE = None


@pytest.fixture
def parent_session(monkeypatch, isolated_home) -> str:
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: object()
    )
    pid = "parent1"
    server_shared._SESSIONS[pid] = _main_session(pid)
    _put_meta(_main_meta(pid))
    return pid


@pytest.fixture
def radio(monkeypatch):
    state: dict = {"sent": []}

    async def fake_push(session_id, event, data, turn_id=None):
        state["sent"].append((session_id, event, data))

    async def fake_turn(session_id, text, extra_kwargs, stop_evt=None):
        state["turns"].append((session_id, text))

    state["turns"] = []
    monkeypatch.setattr(sched, "_push_session_event", fake_push)
    monkeypatch.setattr(plan_mod, "_push_session_event", fake_push)
    monkeypatch.setattr(sched, "_run_managed_turn", fake_turn)
    return state


def _notices(state: dict) -> list[str]:
    return [d["message"] for _s, e, d in state["sent"] if e == "notice"]


def _seed_checkpoint(slug: str, sid: str, msgs: list) -> None:
    """A parent-side checkpoint the fork copies from (a full entry, exactly
    what one real turn leaves behind — shape-compatible with aget_tuple)."""
    cp = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "channel_values": {"messages": msgs},
        "channel_versions": {"messages": 1},
        "versions_seen": {},
    }
    FileCheckpointer(slug).put(
        {"configurable": {"thread_id": sid}}, cp, {"source": "input", "step": 0}, {}
    )


async def _child_messages(sid: str) -> list:
    tup = await FileCheckpointer(SLUG).aget_tuple({"configurable": {"thread_id": sid}})
    assert tup is not None
    return list(((tup.checkpoint.get("channel_values") or {}).get("messages")) or [])


# --------------------------------------------------------------------------- #
# contract 1: agent_type routing
# --------------------------------------------------------------------------- #
async def test_agent_type_hit_prepends_persona_and_tightens_tools(
    isolated_home, parent_session, radio
):
    _write_type("researcher", tools_allow="[read_file, glob_*]")
    res = await sched.create_subagent(
        parent_session, "调研三个库", agent_type="researcher"
    )
    assert res["ok"]
    await asyncio.sleep(0)
    child = res["session_id"]

    meta, _ = _find_meta(child)
    assert meta["subagent"]["agent_type"] == "researcher"
    assert meta["subagent"]["mode"] == "standard"
    # the brief turn carries the persona body BEFORE the envelope
    brief = radio["turns"][0][1]
    assert brief.index("你是只读调研员。") < brief.index("<ginno_subagent_brief>")
    # toolset tightened AT BUILD TIME: the bound roster only holds matches
    entry = server_shared._SESSIONS[child]
    assert entry["restrict_tools"] == ["read_file", "glob_*"]
    import fnmatch

    assert entry["all_tool_names"], "toolset must not be empty"
    for n in entry["all_tool_names"]:
        assert any(fnmatch.fnmatch(n, p) for p in ("read_file", "glob_*"))
    # the session survives a rebuild with the same restriction (meta-derived)
    server_shared._SESSIONS.pop(child)
    from ginno_runtime.api.sessions import _ensure_session

    rebuilt = _ensure_session(child)
    assert rebuilt["restrict_tools"] == ["read_file", "glob_*"]
    assert rebuilt["all_tool_names"] == entry["all_tool_names"]


async def test_agent_type_unknown_lists_available(isolated_home, parent_session, radio):
    _write_type("researcher")
    res = await sched.create_subagent(parent_session, "调研", agent_type="ghost")
    assert not res["ok"]
    assert res["error"].startswith("[error] 未知 subagent 类型")
    assert "researcher" in res["error"]  # the available list rides along
    assert radio["turns"] == []
    assert server_shared.subagent_children(parent_session) == []


async def test_no_agent_type_matches_p2_behavior(isolated_home, parent_session, radio):
    res = await sched.create_subagent(parent_session, "普通委派")
    assert res["ok"]
    await asyncio.sleep(0)
    child = res["session_id"]
    meta, _ = _find_meta(child)
    assert meta["subagent"]["agent_type"] == ""
    assert meta["subagent"]["mode"] == "standard"
    brief = radio["turns"][0][1]
    assert brief.startswith("<ginno_subagent_brief>")
    assert server_shared._SESSIONS[child]["restrict_tools"] == []


async def test_agent_type_model_override(isolated_home, parent_session, radio):
    _write_type("big", model="custom/super-model")
    res = await sched.create_subagent(parent_session, "用指定模型", agent_type="big")
    child = res["session_id"]
    entry = server_shared._SESSIONS[child]
    assert entry["model_provider"] == "custom"
    assert entry["model_name"] == "super-model"
    meta, _ = _find_meta(child)
    assert meta["model"] == "super-model"


async def test_spawn_tool_fork_child_refuses_refork(isolated_home, monkeypatch):
    """P3 contract 2: a fork child calling spawn_subagent(fork=true) gets the
    [error] from the TOOL layer (the scheduler enforces the same rule)."""
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: object()
    )
    _put_meta(_sub_meta("FK", parent="ROOT", depth=0, mode="fork"))
    _put_meta(_main_meta("ROOT"))
    server_shared._SESSIONS["FK"] = {**_main_session("FK"), "type": "subagent"}

    from ginno_runtime.tools.subagent import build_subagent_tools

    tools = {t.name: t for t in build_subagent_tools("FK", SLUG, subagent_depth=0)}
    out = await tools["spawn_subagent"].ainvoke({"goal": "再分一支", "fork": True})
    assert out.startswith("[error]") and "fork" in out
    # a standard spawn from the same fork child is untouched
    ok = await tools["spawn_subagent"].ainvoke({"goal": "标准委派"})
    assert ok.startswith("subagent 已启动")


# --------------------------------------------------------------------------- #
# contract 2: fork
# --------------------------------------------------------------------------- #
async def test_fork_copies_history_with_fresh_ids_and_brief_on_top(
    isolated_home, parent_session, radio
):
    parent_msgs = [
        HumanMessage(content="父问题一", id="h1"),
        AIMessage(
            content="父回答一",
            id="a1",
            tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "tc1"}],
        ),
        ToolMessage(content="文件内容", tool_call_id="tc1", id="t1"),
        HumanMessage(content="父问题二", id="h2"),
        AIMessage(content="父结论", id="a2"),
    ]
    _seed_checkpoint(SLUG, parent_session, parent_msgs)

    res = await sched.create_subagent(
        parent_session, "并行分支任务", fork=True
    )
    assert res["ok"]
    await asyncio.sleep(0)
    child = res["session_id"]

    meta, _ = _find_meta(child)
    assert meta["subagent"]["mode"] == "fork"

    msgs = await _child_messages(child)
    # brief first, then every copied message
    assert "并行分支" in str(msgs[0].content)
    assert "<ginno_subagent_brief>" in str(msgs[0].content)
    assert "从父对话分出的并行分支" in str(msgs[0].content)
    assert len(msgs) == len(parent_msgs) + 1
    for src, cp in zip(parent_msgs, msgs[1:]):
        assert cp.content == src.content
        assert cp.id != src.id  # every id freshly minted
    copied = [m.id for m in msgs]
    assert len(set(copied)) == len(copied)  # and unique among themselves
    # tool_calls / tool_call_id survive (the tool round-trips stay coherent)
    assert msgs[2].tool_calls == parent_msgs[1].tool_calls
    assert msgs[3].tool_call_id == "tc1"
    # the first turn is the fork start instruction, NOT a duplicate brief
    assert radio["turns"][0][1].startswith("这是从父对话分出的并行分支")
    # SystemMessages are never carried over (the model layer adds its own)
    from langchain_core.messages import SystemMessage

    assert not any(isinstance(m, SystemMessage) for m in msgs)


async def test_fork_child_cannot_fork(isolated_home, parent_session, radio):
    res = await sched.create_subagent(parent_session, "第一代 fork", fork=True)
    assert res["ok"]
    child = res["session_id"]
    res2 = await sched.create_subagent(child, "第二代 fork", fork=True)
    assert not res2["ok"]
    assert "fork 子代不能再 fork" in res2["error"]
    assert server_shared.subagent_children(child) == []


async def test_fork_does_not_change_depth_rules(isolated_home, parent_session, radio):
    res = await sched.create_subagent(parent_session, "fork 一层", fork=True)
    assert res["ok"] and res["depth"] == 0
    child = res["session_id"]
    # depth rules are orthogonal: a fork child at depth 0 spawns at depth 1
    res2 = await sched.create_subagent(child, "标准子代", fork=False)
    assert res2["ok"] and res2["depth"] == 1
    # and the structural depth cap still applies from a depth-2 session
    _put_meta(_sub_meta("D2K", parent="D2", depth=2))
    res3 = await sched.create_subagent("D2K", "越界 fork", fork=True)
    assert not res3["ok"] and "深度" in res3["error"]


async def test_fork_seed_failure_degrades_to_standard(
    isolated_home, parent_session, radio, monkeypatch
):
    async def boom(parent_id, child_id, slug, brief):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(sched, "_seed_fork_history", boom)
    res = await sched.create_subagent(parent_session, "fork 兜底", fork=True)
    assert res["ok"]
    await asyncio.sleep(0)
    meta, _ = _find_meta(res["session_id"])
    assert meta["subagent"]["mode"] == "fork"  # still recorded as a fork
    # the brief ran as an ordinary first turn instead
    assert "<ginno_subagent_brief>" in radio["turns"][0][1]


# --------------------------------------------------------------------------- #
# contract 3: max_concurrent (settings + env, dynamic)
# --------------------------------------------------------------------------- #
def test_max_concurrent_default_and_settings(isolated_home, monkeypatch):
    monkeypatch.delenv("GINNO_SUBAGENT_MAX", raising=False)
    assert sched.max_concurrent() == 5
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"subagent": {"max_concurrent": 3}}), encoding="utf-8")
    assert sched.max_concurrent() == 3  # dynamic: no restart needed
    p.write_text(json.dumps({"subagent": {"max_concurrent": 0}}), encoding="utf-8")
    assert sched.max_concurrent() == 1  # clamped
    p.write_text(json.dumps({"subagent": {"max_concurrent": 99}}), encoding="utf-8")
    assert sched.max_concurrent() == 16  # clamped
    p.write_text("{not json", encoding="utf-8")
    assert sched.max_concurrent() == 5  # unreadable settings → default


def test_max_concurrent_env_overrides_everything(isolated_home, monkeypatch):
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"subagent": {"max_concurrent": 3}}), encoding="utf-8")
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "9")
    assert sched.max_concurrent() == 9
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "not-a-number")
    assert sched.max_concurrent() == 3  # bad env falls back to settings
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "200")
    assert sched.max_concurrent() == 16


async def test_spawn_cap_uses_live_setting(isolated_home, parent_session, radio, monkeypatch):
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "1")
    _put_meta(_sub_meta("busy1", parent="other", status="running"))
    res = await sched.create_subagent(parent_session, "第 2 个")
    assert not res["ok"] and res.get("limit")
    assert "已达上限（1）" in res["error"]


async def test_plan_validation_uses_live_setting(isolated_home, monkeypatch):
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "2")
    subtasks, err = plan_mod.validate_subtasks(
        [{"goal": f"g{i}", "reason": "r"} for i in range(3)]
    )
    assert subtasks is None and "上限（2）" in err
    subtasks, err = plan_mod.validate_subtasks(
        [{"goal": f"g{i}", "reason": "r"} for i in range(2)]
    )
    assert subtasks is not None and err == ""


# --------------------------------------------------------------------------- #
# contract 4: soft warnings, once per session each
# --------------------------------------------------------------------------- #
async def test_concurrency_warning_at_80pct_once(isolated_home, parent_session, radio, monkeypatch):
    monkeypatch.setenv("GINNO_SUBAGENT_MAX", "10")  # floor = ceil(8) = 8
    for i in range(7):
        _put_meta(_sub_meta(f"wr{i}", parent="other", status="running"))
    res = await sched.create_subagent(parent_session, "第 8 个")
    assert res["ok"]
    notices = _notices(radio)
    assert len(notices) == 1
    assert "8 个" in notices[0] and "10" in notices[0]
    # a second spawn past the floor does NOT re-warn (once per session)
    _put_meta(_sub_meta("wr7", parent="other", status="running"))
    res2 = await sched.create_subagent(parent_session, "第 9 个")
    assert res2["ok"]
    assert len(_notices(radio)) == 1
    # a DIFFERENT owner session gets its own (single) warning at the same
    # floor (the two children spawned above count toward running too)
    server_shared._SESSIONS["parent2"] = _main_session("parent2")
    _put_meta(_main_meta("parent2"))
    assert await sched._maybe_warn_concurrency("parent2") is True
    notices = _notices(radio)
    assert len(notices) == 2
    assert "10 个" in notices[1]
    # ...and only once for that owner as well
    assert await sched._maybe_warn_concurrency("parent2") is False
    assert len(_notices(radio)) == 2


async def test_usage_warning_at_500k_once(isolated_home, radio):
    _put_meta(_main_meta("ROOT"))
    _put_meta(_sub_meta("UC", parent="ROOT", status="done"))
    _put_meta(_sub_meta("UG", parent="UC", depth=1, status="done"))
    from ginno_runtime.usage_store import record

    record(
        input_tokens=300_000, output_tokens=250_000, provider="p", model="m",
        source="subagent", session_id="UC", project_slug=SLUG,
    )
    record(
        input_tokens=10_000, output_tokens=5_000, provider="p", model="m",
        source="subagent", session_id="UG", project_slug=SLUG,
    )

    fired = await sched._maybe_warn_usage("ROOT", SLUG)
    assert fired
    notices = _notices(radio)
    assert len(notices) == 1
    assert "565000" in notices[0].replace(",", "")  # 300k+250k+10k+5k
    assert "500000" in notices[0].replace(",", "")
    # once per session: the repeat call no-ops the notice
    assert await sched._maybe_warn_usage("ROOT", SLUG) is False
    assert len(_notices(radio)) == 1
    # below threshold: quiet
    _put_meta(_main_meta("ROOT2"))
    _put_meta(_sub_meta("UC2", parent="ROOT2", status="done"))
    record(
        input_tokens=100, output_tokens=100, provider="p", model="m",
        source="subagent", session_id="UC2", project_slug=SLUG,
    )
    assert await sched._maybe_warn_usage("ROOT2", SLUG) is False
    assert len(_notices(radio)) == 1


async def test_done_finalize_checks_usage_warning(isolated_home, radio, monkeypatch):
    """The trigger is wired into the completion gate: a child finalizing done
    re-checks its owner's cumulative usage."""
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model("标题")
    )
    _put_meta(_main_meta("ROOT"))
    _put_meta(_sub_meta("UF", parent="ROOT", status="running"))
    from ginno_runtime.usage_store import record

    record(
        input_tokens=490_000, output_tokens=20_000, provider="p", model="m",
        source="subagent", session_id="UF", project_slug=SLUG,
    )
    await sched.on_turn_settled("UF")
    notices = _notices(radio)
    assert any("510000" in n.replace(",", "") for n in notices)


# --------------------------------------------------------------------------- #
# contract 5: P2 leftovers
# --------------------------------------------------------------------------- #
async def test_background_command_does_not_block_caller(monkeypatch, radio):
    """run_background_command = the WS branch's job: the handler (a decompose
    LLM call in production) runs while the CALLER keeps executing — the
    receive loop stays free for ping/stop/steer."""
    from ginno_runtime.commands import registry as cmd_reg

    assert "subagent" in plan_mod.BACKGROUND_ASYNC_COMMANDS
    assert "subagent-split" in plan_mod.BACKGROUND_ASYNC_COMMANDS

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_handler(project_slug=None, session=None, args=None):
        started.set()
        await release.wait()
        return "已生成拆分方案（2 个子任务）"

    original = cmd_reg.BUILTINS["subagent"]
    cmd_reg.BUILTINS["subagent"] = BuiltinCommand(
        name="subagent",
        description=original.description,
        handler=original.handler,
        async_handler=slow_handler,
    )
    try:
        session = _main_session("BG1")
        job = asyncio.create_task(
            plan_mod.run_background_command(session, "subagent", "拆分 任务", "t1")
        )
        await started.wait()
        assert not job.done()  # the caller was NOT blocked by the handler
        # ...and the loop can do other work meanwhile (the ping/stop guarantee)
        await asyncio.sleep(0)
        release.set()
        await job
    finally:
        cmd_reg.BUILTINS["subagent"] = original
    notices = _notices(radio)
    assert notices == ["已生成拆分方案（2 个子任务）"]


async def test_background_command_reports_handler_failure(monkeypatch, radio):
    from ginno_runtime.commands import registry as cmd_reg

    async def boom(project_slug=None, session=None, args=None):
        raise RuntimeError("模型网关超时")

    monkeypatch.setitem(
        cmd_reg.BUILTINS,
        "subagent-split",
        BuiltinCommand(name="subagent-split", description="", handler=None,
                       async_handler=boom),
    )
    await plan_mod.run_background_command(_main_session("BG2"), "subagent-split", "任务")
    notices = _notices(radio)
    assert notices and "subagent-split 执行失败" in notices[-1]
    assert "模型网关超时" in notices[-1]


def _text_model(text: str) -> Any:
    class _M:
        async def ainvoke(self, messages):
            return AIMessage(content=text)

    return _M()


async def test_cancel_broadcasts_cancelled(isolated_home, monkeypatch, radio):
    monkeypatch.setattr(
        "ginno_runtime.models.build_model",
        lambda *a, **k: _text_model(
            json.dumps([{"goal": "g1", "reason": "r1"}, {"goal": "g2", "reason": "r2"}])
        ),
    )
    session = _main_session("CC1")
    await plan_mod.decompose_and_issue(session, "任务")  # issue a pending plan
    pending = plan_mod.get_pending_plan("CC1")
    assert pending is not None

    cancelled = await plan_mod.cancel_plan_and_broadcast("CC1", pending["plan_id"])
    assert cancelled is True
    assert plan_mod.get_pending_plan("CC1") is None
    ev = [d for _s, e, d in radio["sent"] if e == "subagent.plan.cancelled"]
    assert ev and ev[-1]["plan_id"] == pending["plan_id"]

    # idempotent end to end: an already-consumed id still broadcasts (every
    # tab's pending card must clear), but reports no-op
    cancelled2 = await plan_mod.cancel_plan_and_broadcast("CC1", pending["plan_id"])
    assert cancelled2 is False
    ev = [d for _s, e, d in radio["sent"] if e == "subagent.plan.cancelled"]
    assert len(ev) == 2


# --------------------------------------------------------------------------- #
# review fixes: fork tail heal + orphan reconciliation
# --------------------------------------------------------------------------- #
async def test_fork_heals_dangling_tool_call_tail(
    isolated_home, parent_session, radio
):
    """A fork runs from inside the parent's LIVE turn, so the checkpoint being
    copied ends with the AIMessage whose tool_calls (spawn_subagent's own,
    plus parallel batch mates) have no ToolMessage answers yet. Seeding that
    tail verbatim 400s every turn of the fork child (tool_use without
    tool_result) — the seed must append "(interrupted)" placeholders."""
    parent_msgs = [
        HumanMessage(content="父问题", id="h1"),
        AIMessage(content="父回答", id="a1"),
        AIMessage(
            content="",
            id="a2",
            tool_calls=[
                {"name": "spawn_subagent", "args": {"goal": "x"}, "id": "tcS"},
                {"name": "list_subagents", "args": {}, "id": "tcL"},
            ],
        ),
    ]
    _seed_checkpoint(SLUG, parent_session, parent_msgs)

    res = await sched.create_subagent(parent_session, "fork 任务", fork=True)
    assert res["ok"]
    await asyncio.sleep(0)
    msgs = await _child_messages(res["session_id"])

    # the copied tail is complete: every tool_call id in the child history has
    # a following ToolMessage answer
    answers = {
        m.tool_call_id for m in msgs if isinstance(m, ToolMessage)
    }
    asked = {
        tc["id"]
        for m in msgs
        if isinstance(m, AIMessage)
        for tc in (getattr(m, "tool_calls", None) or [])
        if isinstance(tc, dict) and tc.get("id")
    }
    assert asked == {"tcS", "tcL"}  # batch copied faithfully
    assert asked <= answers  # and fully answered by placeholders
    tails = [m for m in msgs if isinstance(m, ToolMessage)][-2:]
    assert [t.tool_call_id for t in tails] == ["tcS", "tcL"]
    assert all(t.content == "(interrupted)" for t in tails)
    # placeholders carry fresh fork- ids (delta id discipline)
    assert all((t.id or "").startswith("fork-") for t in tails)

    # control: a clean parent tail gains NO placeholder
    _seed_checkpoint(
        SLUG,
        "parent-clean",
        [
            HumanMessage(content="q", id="c1"),
            AIMessage(
                content="a",
                id="c2",
                tool_calls=[{"name": "read_file", "args": {}, "id": "ctc"}],
            ),
            ToolMessage(content="body", tool_call_id="ctc", id="ct1"),
        ],
    )
    server_shared._SESSIONS["parent-clean"] = _main_session("parent-clean")
    _put_meta(_main_meta("parent-clean"))
    res2 = await sched.create_subagent("parent-clean", "干净尾巴", fork=True)
    assert res2["ok"]
    await asyncio.sleep(0)
    msgs2 = await _child_messages(res2["session_id"])
    answers2 = {m.tool_call_id for m in msgs2 if isinstance(m, ToolMessage)}
    assert answers2 == {"ctc"}  # the real answer, no extra placeholder


def _put_sub_meta(sid, parent, depth, status, mode=None):
    _put_meta(_sub_meta(sid, parent=parent, depth=depth, status=status, mode=mode))


def test_reconcile_orphan_subagents_settles_live_metas(isolated_home):
    """Startup backstop: running/waiting metas stranded by the previous
    process are settled "stopped" (freeing concurrency slots); terminal metas
    are untouched; the concurrency count no longer sees the ghosts."""
    _put_meta(_main_meta("R1"))
    _put_sub_meta("R1A", "R1", 0, "running")
    _put_sub_meta("R1B", "R1", 0, "waiting")
    _put_sub_meta("R1C", "R1", 0, "done")
    _put_meta(_main_meta("R2"))
    _put_sub_meta("R2A", "R2", 0, "running")
    # a ghost under a stopped parent settles independently (no injection, so
    # tree order genuinely does not matter)
    _put_sub_meta("R1D", "R1", 1, "running")
    _put_sub_meta("R1E", "R1D", 2, "waiting")

    assert len(sched._running_subagent_metas()) == 3  # ghosts counted before
    settled = sched.reconcile_orphan_subagents()
    assert settled == 5
    for sid in ("R1A", "R1B", "R1D", "R1E"):
        found = _find_meta(sid)
        assert found and found[0]["subagent"]["status"] == "stopped"
    found = _find_meta("R1C")
    assert found and found[0]["subagent"]["status"] == "done"  # terminal kept
    assert sched._running_subagent_metas() == []  # cap freed
    # idempotent: a second pass has nothing to do
    assert sched.reconcile_orphan_subagents() == 0
