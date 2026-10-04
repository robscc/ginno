"""Unit tests for the subagent P2 command/plan/stop surface.

Coverage per the P2 shared contract:

* ``/subagent`` two forms + the ``/subagent-split`` alias (contract 3): the
  resolver routes all of them to the busy-gated async path; the direct form
  spawns one subagent with ``origin="user"``; the split form runs the LLM
  decompose with STRICT validation (non-array / missing goal / missing
  reason / over the concurrency cap → error notice, NO plan);
* ``subagent.plan`` issue/confirm/cancel (contracts 1-2): the plan broadcast,
  confirm spawning each subtask through the existing path, the edited-card
  subtasks winning, cancel discarding, a re-split overwriting the old plan;
* ``POST /api/sessions/{id}/stop`` (contract 4): running → cooperative stop
  event; parked → heal path; waiting subagent → tree stop; idle → 202 no-op;
* the acceptance line in the result injection (contract 5);
* the LLM title after the first turn (contract 6), including both degradation
  paths (GINNO_FAKE_LLM skip, unusable model answer keeps goal[:40]).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from ginno_runtime import paths, server_shared
from ginno_runtime import subagent_plan as plan_mod
from ginno_runtime import subagent_scheduler as sched
from ginno_runtime.api import sessions as sessions_api
from ginno_runtime.api import stream as stream_mod
from ginno_runtime.api.sessions import stop_session
from ginno_runtime.commands import BUILTINS, resolve_turn
from ginno_runtime.commands.registry import _subagent_async_handler
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
        server_shared._PENDING_RESUME,
        sched._FINALIZING,
        plan_mod._PENDING_PLANS,
    )
    for r in regs:
        r.clear()
    yield
    for r in regs:
        r.clear()


@pytest.fixture(autouse=True)
def _fresh_locks():
    sched._SPAWN_LOCK = asyncio.Lock()
    yield


def _put_meta(meta: dict) -> None:
    paths.project_sessions_dir(SLUG).mkdir(parents=True, exist_ok=True)
    _session_meta_upsert(SLUG, meta)


def _main_session(sid: str) -> dict:
    """Minimal in-memory main-conversation entry."""
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


def _text_model(text: str) -> Any:
    class _M:
        async def ainvoke(self, messages):
            return AIMessage(content=text)

    return _M()


@pytest.fixture
def radio(monkeypatch):
    """Record WS pushes from BOTH the plan module and the scheduler."""
    state: dict = {"sent": []}

    async def fake_push(session_id, event, data, turn_id=None):
        state["sent"].append((session_id, event, data))

    monkeypatch.setattr(plan_mod, "_push_session_event", fake_push)
    monkeypatch.setattr(sched, "_push_session_event", fake_push)
    return state


@pytest.fixture
def fake_turn(monkeypatch):
    """Fake the scheduler-managed turn runner (spawn briefs / wake turns)."""
    state: dict = {"turns": []}

    async def fake(session_id, text, extra_kwargs, stop_evt=None):
        state["turns"].append((session_id, text))

    monkeypatch.setattr(sched, "_run_managed_turn", fake)
    return state


@pytest.fixture
def offline_spawn(monkeypatch):
    """Patch the session model build so create_subagent stays offline."""
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: object()
    )


def _plan_event(radio_state: dict) -> dict | None:
    for _sid, ev, data in radio_state["sent"]:
        if ev == "subagent.plan":
            return data
    return None


# --------------------------------------------------------------------------- #
# resolver routing (contract 3)
# --------------------------------------------------------------------------- #
def test_resolver_routes_subagent_family_to_async():
    session = _main_session("R1")
    plan = resolve_turn({"message": "/subagent 直接目标"}, session)
    assert plan.builtin_async == "subagent" and plan.builtin_args == "直接目标"
    assert plan.builtin_reply is None  # never the sync path
    plan = resolve_turn({"message": "/subagent 拆分 一个大任务"}, session)
    assert plan.builtin_async == "subagent" and "一个大任务" in plan.builtin_args
    plan = resolve_turn({"message": "/subagent-split 评审任务"}, session)
    assert plan.builtin_async == "subagent-split" and plan.builtin_args == "评审任务"
    # membership: both names are builtins, an unknown /word still passes through
    assert "subagent-split" in BUILTINS


# --------------------------------------------------------------------------- #
# direct form (contract 3)
# --------------------------------------------------------------------------- #
async def test_direct_form_spawns_one_subagent_as_user(
    isolated_home, offline_spawn, fake_turn, radio
):
    _put_meta({"id": "M1", "title": "main", "created": time.time(), "updated": time.time()})
    server_shared._SESSIONS["M1"] = _main_session("M1")

    reply = await _subagent_async_handler(SLUG, server_shared._SESSIONS["M1"], "调研 OAuth 库")
    await asyncio.sleep(0)  # let the spawn_bg brief-turn task run

    assert "✅ subagent started" in reply
    children = server_shared.subagent_children("M1")
    assert len(children) == 1
    meta, _ = _find_meta(children[0])
    assert meta["subagent"]["origin"] == "user"
    assert meta["subagent"]["goal"] == "调研 OAuth 库"
    assert meta["subagent"]["constraints"] == "" and meta["subagent"]["acceptance"] == ""
    assert fake_turn["turns"] and fake_turn["turns"][0][0] == children[0]


async def test_subagent_without_args_returns_usage(isolated_home):
    assert "usage" in await _subagent_async_handler(SLUG, _main_session("M2"), "")


# --------------------------------------------------------------------------- #
# split form: decompose + strict validation (contract 3)
# --------------------------------------------------------------------------- #
_VALID = [
    {"goal": "调研 OAuth 库", "constraints": "只读", "acceptance": "给出选型结论", "reason": "纯只读调研，独立上下文即可完成"},
    {"goal": "编写集成测试骨架", "reason": "与调研无文件冲突"},
]


async def test_split_form_issues_plan_and_broadcasts(isolated_home, monkeypatch, radio):
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model(json.dumps(_VALID, ensure_ascii=False))
    )
    session = _main_session("S1")

    reply = await _subagent_async_handler(SLUG, session, "拆分 完成认证模块重构")

    assert "Split plan" in reply and "rationale" in reply
    pending = plan_mod.get_pending_plan("S1")
    assert pending is not None
    assert pending["task"] == "完成认证模块重构"
    assert [st["goal"] for st in pending["subtasks"]] == ["调研 OAuth 库", "编写集成测试骨架"]
    ev = _plan_event(radio)
    # contract 1: the plan frame carries plan_id + the four subtask fields
    assert ev and ev["session_id"] == "S1" and ev["plan_id"] == pending["plan_id"]
    assert ev["subtasks"][0]["reason"].startswith("纯只读调研")


async def test_decompose_tolerates_prefill_echo_and_fences(isolated_home, monkeypatch, radio):
    # The model echoes the "[" prefill and wraps in a code fence.
    raw = "```json\n" + json.dumps(_VALID, ensure_ascii=False) + "\n```"
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model(raw)
    )
    await _subagent_async_handler(SLUG, _main_session("S2"), "拆分 任务")
    assert plan_mod.get_pending_plan("S2") is not None


@pytest.mark.parametrize(
    "raw,frag",
    [
        ('{"goal": "不是数组"}', "JSON array"),
        ("前言解释了一大堆，没有 JSON", "JSON array"),
        (json.dumps([{"goal": "缺理由"}], ensure_ascii=False), "reason"),
        (json.dumps([{"reason": "缺 goal"}], ensure_ascii=False), "goal"),
        (json.dumps([{"goal": " ", "reason": "空 goal"}], ensure_ascii=False), "goal"),
        ("[]", "produced no subtasks"),
    ],
)
async def test_decompose_invalid_output_never_issues_plan(
    isolated_home, monkeypatch, radio, raw, frag
):
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model(raw)
    )
    reply = await _subagent_async_handler(SLUG, _main_session("S3"), "拆分 任务")
    assert reply.startswith("Split failed: ") and frag in reply
    assert plan_mod.get_pending_plan("S3") is None
    assert _plan_event(radio) is None  # no half-built plan is ever broadcast


async def test_decompose_rejects_more_than_concurrency_cap(isolated_home, monkeypatch, radio):
    many = [{"goal": f"g{i}", "reason": "r"} for i in range(sched.SUBAGENT_MAX_CONCURRENT + 1)]
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model(json.dumps(many, ensure_ascii=False))
    )
    reply = await _subagent_async_handler(SLUG, _main_session("S4"), "拆分 大任务")
    assert "cap" in reply
    assert plan_mod.get_pending_plan("S4") is None


async def test_decompose_unavailable_under_fake_llm(isolated_home, monkeypatch, radio):
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    reply = await _subagent_async_handler(SLUG, _main_session("S5"), "拆分 任务")
    assert reply.startswith("Split failed: ")
    assert plan_mod.get_pending_plan("S5") is None


async def test_split_alias_handler(isolated_home, monkeypatch, radio):
    from ginno_runtime.commands.registry import _subagent_split_async_handler

    monkeypatch.setattr(
        "ginno_runtime.models.build_model",
        lambda *a, **k: _text_model(json.dumps(_VALID, ensure_ascii=False)),
    )
    reply = await _subagent_split_async_handler(SLUG, _main_session("S6"), "评审任务描述")
    assert "Split plan" in reply
    assert plan_mod.get_pending_plan("S6")["task"] == "评审任务描述"


# --------------------------------------------------------------------------- #
# plan confirm / cancel (contract 2)
# --------------------------------------------------------------------------- #
async def _issue(monkeypatch, sid: str, task: str = "任务") -> dict:
    monkeypatch.setattr(
        "ginno_runtime.models.build_model",
        lambda *a, **k: _text_model(json.dumps(_VALID, ensure_ascii=False)),
    )
    await _subagent_async_handler(SLUG, _main_session(sid), f"拆分 {task}")
    return plan_mod.get_pending_plan(sid)


async def test_confirm_spawns_each_subtask_as_user(
    isolated_home, offline_spawn, fake_turn, radio, monkeypatch
):
    _put_meta({"id": "C1", "title": "main", "created": time.time(), "updated": time.time()})
    server_shared._SESSIONS["C1"] = _main_session("C1")
    plan = await _issue(monkeypatch, "C1")

    reply = await plan_mod.confirm_plan("C1", plan["plan_id"])
    await asyncio.sleep(0)  # let the spawn_bg brief-turn tasks run

    assert "2 subagent(s) started" in reply
    children = server_shared.subagent_children("C1")
    assert len(children) == 2
    for cid in children:
        meta, _ = _find_meta(cid)
        assert meta["subagent"]["origin"] == "user"
        assert meta["subagent"]["status"] == "running"
    assert len(fake_turn["turns"]) == 2  # each child got its brief turn
    notices = [d for _s, e, d in radio["sent"] if e == "notice"]
    assert notices and "2 subagent(s) started" in notices[-1]["message"]
    assert plan_mod.get_pending_plan("C1") is None  # consumed


async def test_confirm_with_edited_subtasks_overrides(isolated_home, offline_spawn, fake_turn, radio, monkeypatch):
    _put_meta({"id": "C2", "title": "main", "created": time.time(), "updated": time.time()})
    server_shared._SESSIONS["C2"] = _main_session("C2")
    plan = await _issue(monkeypatch, "C2")

    edited = [{"goal": "编辑后的子任务", "constraints": "只读", "acceptance": "有结论", "reason": "用户改过"}]
    reply = await plan_mod.confirm_plan("C2", plan["plan_id"], edited)

    assert "1 subagent(s) started" in reply
    meta, _ = _find_meta(server_shared.subagent_children("C2")[0])
    assert meta["subagent"]["goal"] == "编辑后的子任务"


async def test_confirm_rejects_invalid_edit(isolated_home, monkeypatch, radio):
    plan = await _issue(monkeypatch, "C3")
    reply = await plan_mod.confirm_plan("C3", plan["plan_id"], [{"goal": "缺理由"}])
    assert "invalid" in reply
    # the plan survives a rejected edit — the user can fix the card and retry
    assert plan_mod.get_pending_plan("C3") is not None


async def test_confirm_unknown_plan_id_errors(isolated_home, radio):
    reply = await plan_mod.confirm_plan("C4", "nope")
    assert "no pending plan" in reply


async def test_cancel_discards_plan(isolated_home, monkeypatch, radio):
    plan = await _issue(monkeypatch, "C5")
    assert plan_mod.cancel_plan("C5", plan["plan_id"]) is True
    assert plan_mod.cancel_plan("C5", plan["plan_id"]) is False  # idempotent
    reply = await plan_mod.confirm_plan("C5", plan["plan_id"])
    assert "no pending plan" in reply


async def test_confirm_refusal_pushes_notice(isolated_home, monkeypatch, radio):
    """The confirming tab flips its card to 「已确认」 BEFORE the server answers;
    the WS handler drops confirm_plan's return value — so a confirm that raced
    a cancel/overwrite (second tab) must come back as a notice event, or the
    whole batch silently never spawns."""
    plan = await _issue(monkeypatch, "C9")
    plan_mod.cancel_plan("C9", plan["plan_id"])
    reply = await plan_mod.confirm_plan("C9", plan["plan_id"])
    assert "no pending plan" in reply
    notices = [d for _s, e, d in radio["sent"] if e == "notice"]
    assert notices and "did not take effect" in notices[-1]["message"]
    # same for a rejected edit: plan survives, refusal is broadcast
    plan_b = await _issue(monkeypatch, "C9", "任务B")
    reply2 = await plan_mod.confirm_plan("C9", plan_b["plan_id"], [{"goal": "缺理由"}])
    assert "invalid" in reply2
    notices2 = [d for _s, e, d in radio["sent"] if e == "notice"]
    assert "Edited subtasks are invalid" in notices2[-1]["message"]
    assert plan_mod.get_pending_plan("C9") is not None  # still fixable


async def test_resplit_overwrites_pending_plan(isolated_home, offline_spawn, fake_turn, radio, monkeypatch):
    plan_a = await _issue(monkeypatch, "C6", "任务A")
    plan_b = await _issue(monkeypatch, "C6", "任务B")
    assert plan_a["plan_id"] != plan_b["plan_id"]
    assert plan_mod.get_pending_plan("C6")["plan_id"] == plan_b["plan_id"]
    # the stale card's confirm is refused
    reply = await plan_mod.confirm_plan("C6", plan_a["plan_id"])
    assert "no pending plan" in reply
    assert plan_mod.get_pending_plan("C6") is not None  # B still pending


# --------------------------------------------------------------------------- #
# POST /api/sessions/{id}/stop (contract 4)
# --------------------------------------------------------------------------- #
async def test_stop_running_sets_cooperative_event(isolated_home, radio):
    _put_meta({"id": "SR1", "title": "t", "created": time.time(), "updated": time.time()})
    server_shared._SESSIONS["SR1"] = _main_session("SR1")
    server_shared._RUNNING_TURNS["SR1"] = "turn-1"
    evt = server_shared._TURN_STOP.setdefault("SR1", asyncio.Event())

    res = await stop_session("SR1")

    assert res["stopped"] is True and res["mode"] == "running"
    assert evt.is_set()


async def test_stop_parked_heals_via_stop_parked_turn(isolated_home, radio, monkeypatch):
    healed: list[tuple[str, str]] = []

    async def fake_parked(session, sid, tid):
        healed.append((sid, tid))

    monkeypatch.setattr(stream_mod, "_stop_parked_turn", fake_parked)
    server_shared._SESSIONS["SP1"] = _main_session("SP1")
    server_shared._PENDING_RESUME.add("SP1")
    server_shared._PENDING_KIND["SP1"] = "permission_request"
    server_shared._RUNNING_TURNS["SP1"] = "turn-p1"

    res = await stop_session("SP1")
    await asyncio.sleep(0)  # let the spawn_bg heal task run

    assert res["mode"] == "parked"
    assert healed == [("SP1", "turn-p1")]
    assert "SP1" not in server_shared._PENDING_RESUME
    assert "SP1" not in server_shared._RUNNING_TURNS


def _sub_meta(sid: str, parent: str | None = None, status: str = "running") -> dict:
    m: dict = {"id": sid, "title": f"t-{sid}", "created": time.time(), "updated": time.time()}
    if parent is not None:
        m.update({
            "type": "subagent",
            "parent_session_id": parent,
            "depth": 0,
            "subagent": {
                "goal": "g", "constraints": "", "acceptance": "",
                "origin": "user", "status": status, "result_summary": "",
            },
        })
    return m


async def test_stop_waiting_subagent_finalizes_tree(isolated_home, radio):
    _put_meta({"id": "SWR", "title": "root", "created": time.time(), "updated": time.time()})
    _put_meta(_sub_meta("SW", parent="SWR", status="waiting"))
    _put_meta(_sub_meta("SWC", parent="SW", status="running"))
    # a waiting subagent holds no turn of its own: nothing in the registries

    res = await stop_session("SW")
    await asyncio.sleep(0)

    assert res["mode"] == "waiting"
    meta, _ = _find_meta("SW")
    assert meta["subagent"]["status"] == "stopped"
    child, _ = _find_meta("SWC")
    assert child["subagent"]["status"] == "stopped"


async def test_stop_idle_is_a_noop_but_202(isolated_home, radio):
    _put_meta({"id": "SI1", "title": "t", "created": time.time(), "updated": time.time()})

    res = await stop_session("SI1")

    assert res["stopped"] is True and res["mode"] == "idle"
    assert "SI1" not in server_shared._TURN_STOP  # never leave a set event


async def test_stop_running_subagent_cascades(isolated_home, radio, monkeypatch):
    _put_meta(_sub_meta("SX"))
    _put_meta(_sub_meta("SXC", parent="SX", status="running"))
    server_shared._SESSIONS["SX"] = {**_main_session("SX"), "type": "subagent"}
    server_shared._RUNNING_TURNS["SX"] = "turn-sx"
    evt = server_shared._TURN_STOP.setdefault("SX", asyncio.Event())
    child_evt = server_shared._TURN_STOP.setdefault("SXC", asyncio.Event())

    res = await stop_session("SX")
    await asyncio.sleep(0)

    assert res["mode"] == "running"
    assert evt.is_set() and child_evt.is_set()


async def test_stop_parked_subagent_cascades_descendants(isolated_home, radio, monkeypatch):
    """HTTP-stop parity with the WS branch (§5.6 配套规则): a subagent parked at
    a permission interrupt is healed to "stopped" by _stop_parked_turn, and its
    still-running descendants must be cascade-stopped with it — previously the
    parked branch skipped the cascade, orphaning them under a stopped parent
    (their results are then refused at the terminal-parent gate and lost)."""
    healed: list[tuple[str, str]] = []

    async def fake_parked(session, sid, tid):
        healed.append((sid, tid))

    monkeypatch.setattr(stream_mod, "_stop_parked_turn", fake_parked)
    _put_meta(_sub_meta("SPX"))
    _put_meta(_sub_meta("SPXC", parent="SPX", status="running"))
    server_shared._SESSIONS["SPX"] = {**_main_session("SPX"), "type": "subagent"}
    server_shared._PENDING_RESUME.add("SPX")
    server_shared._PENDING_KIND["SPX"] = "permission_request"
    server_shared._RUNNING_TURNS["SPX"] = "turn-p"

    res = await stop_session("SPX")
    await asyncio.sleep(0.05)  # let the heal + spawn_bg cascade task settle

    assert res["mode"] == "parked"
    assert healed == [("SPX", "turn-p")]
    child, _ = _find_meta("SPXC")
    assert child["subagent"]["status"] == "stopped"


# --------------------------------------------------------------------------- #
# acceptance line in the result injection (contract 5)
# --------------------------------------------------------------------------- #
def test_result_injection_carries_acceptance_line():
    text = sched.format_subagent_result("kid", "调研", "结论：选 A", "给出选型结论")
    assert "Acceptance criteria: 给出选型结论" in text
    assert "one-line verdict (met / gap + explanation)" in text
    assert text.index("结论：选 A") < text.index("Acceptance criteria:")  # appended AFTER the summary
    assert text.endswith("</ginno_subagent_result>")


def test_result_injection_without_acceptance_is_unchanged():
    text = sched.format_subagent_result("kid", "调研", "结论：选 A", "")
    assert "Acceptance criteria" not in text
    plain = sched.format_subagent_result("kid", "调研", "结论：选 A")
    assert plain == text


# --------------------------------------------------------------------------- #
# LLM title after the first turn (contract 6)
# --------------------------------------------------------------------------- #
def _titled_meta(sid: str) -> dict:
    return _sub_meta(sid, parent="ROOT", status="running")


async def test_title_generated_after_first_done_turn(isolated_home, radio, monkeypatch):
    _put_meta(_titled_meta("T1"))
    monkeypatch.setattr(
        "ginno_runtime.models.build_model", lambda *a, **k: _text_model("调研 OAuth 库选型")
    )

    await sched._gen_subagent_title("T1", "证据与结论……")

    meta, _ = _find_meta("T1")
    assert meta["title"] == "调研 OAuth 库选型"
    assert meta["subagent"]["titled"] is True
    titles = [d for _s, e, d in radio["sent"] if e == "session_title"]
    assert titles and titles[0]["title"] == "调研 OAuth 库选型"


async def test_title_truncated_to_16_chars(isolated_home, radio, monkeypatch):
    _put_meta(_titled_meta("T2"))
    monkeypatch.setattr(
        "ginno_runtime.models.build_model",
        lambda *a, **k: _text_model("调研 OAuth 库并对主流方案的 token 刷新机制给出选型建议"),
    )

    await sched._gen_subagent_title("T2", "报告")

    meta, _ = _find_meta("T2")
    assert len(meta["title"]) <= 16


async def test_title_degrades_to_goal_prefix_on_model_failure(isolated_home, radio, monkeypatch):
    _put_meta(_titled_meta("T3"))

    class _Boom:
        async def ainvoke(self, messages):
            raise RuntimeError("gateway down")

    monkeypatch.setattr("ginno_runtime.models.build_model", lambda *a, **k: _Boom())

    await sched._gen_subagent_title("T3", "报告")

    meta, _ = _find_meta("T3")
    assert meta["title"] == "t-T3"  # untouched placeholder, no crash
    assert "titled" not in meta["subagent"]


async def test_title_skipped_under_fake_llm(isolated_home, radio, monkeypatch):
    _put_meta(_titled_meta("T4"))
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")

    await sched._gen_subagent_title("T4", "报告")

    meta, _ = _find_meta("T4")
    assert meta["title"] == "t-T4"


async def test_title_applied_at_most_once(isolated_home, radio, monkeypatch):
    _put_meta(_titled_meta("T5"))
    calls: list[int] = []

    class _Counting:
        async def ainvoke(self, messages):
            calls.append(1)
            return AIMessage(content="第一个标题")

    monkeypatch.setattr("ginno_runtime.models.build_model", lambda *a, **k: _Counting())

    await sched._gen_subagent_title("T5", "报告")
    await sched._gen_subagent_title("T5", "报告")  # second settle → no-op

    assert len(calls) == 1
    meta, _ = _find_meta("T5")
    assert meta["title"] == "第一个标题"
    titles = [d for _s, e, d in radio["sent"] if e == "session_title"]
    assert len(titles) == 1


async def test_done_finalize_spawns_title_once(isolated_home, fake_turn, radio, monkeypatch):
    """End-to-end gate shape: a done finalize fires the title job, which marks
    the meta titled — a second settle of the same session never re-titles."""
    _put_meta(_titled_meta("T6"))
    _put_meta({"id": "ROOT", "title": "root", "created": time.time(), "updated": time.time()})
    server_shared._SESSIONS["ROOT"] = _main_session("ROOT")
    calls: list[int] = []

    class _Counting:
        async def ainvoke(self, messages):
            calls.append(1)
            return AIMessage(content="完成调研任务")

    monkeypatch.setattr("ginno_runtime.models.build_model", lambda *a, **k: _Counting())
    # keep the report read local (no checkpoint file for T6 → empty report)

    await sched.on_turn_settled("T6")
    await asyncio.sleep(0.05)  # let the spawn_bg title job run

    meta, _ = _find_meta("T6")
    assert meta["subagent"]["status"] == "done"
    assert meta["subagent"]["titled"] is True
    assert len(calls) == 1
    titles = [d for _s, e, d in radio["sent"] if e == "session_title"]
    assert titles and titles[0]["title"] == "完成调研任务"
