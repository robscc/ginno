"""Unit tests for the subagent scheduler + toolset (docs/subagent-design.md).

Coverage per the P1 contract:

* spawn — depth cap (contract 4/6), concurrency hard cap of 5 (contract 5),
  child meta shape (contract 1), the standalone brief (design §5.3 + A.4/A.6)
  and the ``subagent.spawned`` event (contract 2);
* the structural completion gate (contract 6): turn ended + live descendant →
  ``waiting``; last descendant done → ``done`` with the result injected into
  the parent through the RIGHT channel (steer stash when the parent is
  running, a wake turn when idle);
* the injection message format (contract 3), including the persisted
  ``additional_kwargs["ginno_subagent_result"]`` tag;
* the stop cascade over the parent_session_id tree (contract 7), the
  anti-starvation wake for waiting parents, and cascade delete (§5.8).

Session metas are written straight into the per-slug index; the heavy turn
runner and the WS push are monkeypatched — the gate logic under test stays
real.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from ginno_runtime import paths, server_shared
from ginno_runtime import subagent_scheduler as sched
from ginno_runtime.api import stream as stream_mod
from ginno_runtime.server_shared import spawn_bg
from ginno_runtime.session_meta import (
    _session_meta_list,
    _session_meta_patch,
    _session_meta_upsert,
    subagent_depth_of,
)

pytestmark = pytest.mark.unit

SLUG = "default"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_subagent_state():
    """The shared conftest does not know these registries; fixed test ids
    (P/C/G…) would otherwise leak across tests."""
    regs = (
        server_shared._SESSION_CHILDREN,
        server_shared._STEER_STASH,
        server_shared._STEER_INFLIGHT,
        server_shared._RUNNING_TURNS,
        server_shared._TURN_STOP,
        server_shared._TURN_TASKS,
        sched._FINALIZING,
        sched._PENDING_INJECTIONS,
        sched._INJECTION_WAKING,
    )
    for r in regs:
        r.clear()
    yield
    for t in list(sched._INJECTION_FLUSH_TASKS.values()):
        t.cancel()
    sched._INJECTION_FLUSH_TASKS.clear()
    for r in regs:
        r.clear()


@pytest.fixture(autouse=True)
def _fresh_spawn_lock():
    """asyncio.Lock binds to the running loop on first use; a new loop per
    test needs a new lock."""
    sched._SPAWN_LOCK = asyncio.Lock()
    yield


def _put_meta(meta: dict) -> None:
    paths.project_sessions_dir(SLUG).mkdir(parents=True, exist_ok=True)
    _session_meta_upsert(SLUG, meta)


def _sub_meta(
    sid: str,
    parent: str | None = None,
    depth: int = 0,
    status: str = "running",
    goal: str = "调研 OAuth 库",
) -> dict:
    m: dict = {
        "id": sid,
        "title": f"t-{sid}",
        "created": time.time(),
        "updated": time.time(),
    }
    if parent is not None:
        m.update(
            {
                "type": "subagent",
                "parent_session_id": parent,
                "depth": depth,
                "subagent": {
                    "goal": goal,
                    "constraints": "",
                    "acceptance": "",
                    "origin": "agent",
                    "status": status,
                    "result_summary": "",
                },
            }
        )
    return m


@pytest.fixture
def parent_session(monkeypatch, isolated_home) -> str:
    """A main session resolvable by create_subagent, with the model build
    patched so create_session's graph build stays offline."""
    monkeypatch.setattr(
        "ginno_runtime.api.sessions.build_model", lambda *a, **k: object()
    )
    pid = "parent1"
    server_shared._SESSIONS[pid] = {
        "session_id": pid,
        "project_slug": SLUG,
        "workspace": str(isolated_home / "ws"),
        "agent_id": "dev",
        "model_provider": "custom",
        "model_name": None,
        "type": None,
        "context_folders": [],
        "primary_folder": None,
    }
    _put_meta(_sub_meta(pid))
    return pid


@pytest.fixture
def capture(monkeypatch):
    """Recorder for the scheduler's outbound edges: WS pushes + wake turns."""
    state: dict = {"sent": [], "wakes": [], "turns": []}

    async def fake_push(session_id, event, data, turn_id=None):
        state["sent"].append((session_id, event, data))

    async def fake_wake(parent_id, text, extra_kwargs):
        state["wakes"].append((parent_id, text, extra_kwargs))

    async def fake_turn(session_id, text, extra_kwargs, stop_evt):
        state["turns"].append((session_id, text))

    monkeypatch.setattr(sched, "_push_session_event", fake_push)
    monkeypatch.setattr(sched, "_wake_parent_turn", fake_wake)
    monkeypatch.setattr(sched, "_run_managed_turn", fake_turn)
    return state


# --------------------------------------------------------------------------- #
# spawn
# --------------------------------------------------------------------------- #
async def test_spawn_creates_child_meta_brief_and_event(
    isolated_home, parent_session, capture
):
    res = await sched.create_subagent(
        parent_session, "调研 OAuth 库", constraints="只读", acceptance="给出选型结论"
    )
    assert res["ok"] and res["depth"] == 0
    await _ticks()  # let the spawn_bg turn task (faked) run
    child = res["session_id"]

    meta, _ = sched._find_meta(child)
    # contract 1: the meta carries the whole subagent payload
    assert meta["type"] == "subagent"
    assert meta["parent_session_id"] == parent_session
    assert meta["depth"] == 0
    sa = meta["subagent"]
    assert sa["goal"] == "调研 OAuth 库" and sa["constraints"] == "只读"
    assert sa["acceptance"] == "给出选型结论"
    assert sa["origin"] == "agent" and sa["status"] == "running"
    # reverse index
    assert server_shared.subagent_children(parent_session) == [child]
    # the brief ran as the child's first turn (design §5.3 + A.4 + A.6)
    assert capture["turns"] and capture["turns"][0][0] == child
    brief = capture["turns"][0][1]
    # 结构化简报（2026-10-01）：每段一个标签，段名与 spawn_subagent 参数对齐，
    # 前端用 DOMParser 取而非正则切文本
    assert "<ginno_subagent_brief>" in brief and "</ginno_subagent_brief>" in brief
    assert "<goal>调研 OAuth 库</goal>" in brief
    assert "<constraints>只读</constraints>" in brief
    assert "<acceptance>给出选型结论</acceptance>" in brief
    assert "<other>" in brief and "depth-1 subagent" in brief and "may delegate" in brief
    assert "<report_format>" in brief and "Final report format" in brief
    assert "<output_discipline>" in brief and "Output discipline" in brief
    # contract 2: subagent.spawned broadcast to the parent (and the child)
    spawned = [d for _, e, d in capture["sent"] if e == "subagent.spawned"]
    assert spawned and spawned[0]["session_id"] == child
    assert spawned[0]["parent_session_id"] == parent_session
    assert spawned[0]["title"] == res["title"]


async def test_brief_forbids_delegation_at_max_depth(isolated_home):
    brief = sched.build_subagent_brief("g", "", "", sched.SUBAGENT_MAX_DEPTH)
    assert "may NOT delegate" in brief


async def test_brief_sections_are_xml_escaped(isolated_home):
    """段内容可能含 < > &（模型自由文本），必须转义——否则 DOMParser 解析失败，
    前端整条简报退回原文显示。"""
    brief = sched.build_subagent_brief("a<b & c>d", "x<y", "p&q", 0)
    assert "<goal>a&lt;b &amp; c&gt;d</goal>" in brief
    assert "<constraints>x&lt;y</constraints>" in brief
    assert "<acceptance>p&amp;q</acceptance>" in brief


async def test_spawn_rejects_depth_overflow(isolated_home, parent_session):
    _put_meta(
        {
            **_sub_meta("parent1"),
            "type": "subagent",
            "parent_session_id": "root",
            "depth": 2,
            "subagent": _sub_meta("x", parent="root", depth=2)["subagent"],
        }
    )
    res = await sched.create_subagent(parent_session, "再拆一层")
    assert not res["ok"]
    assert "Maximum nesting depth" in res["error"]
    # nothing was created
    assert server_shared.subagent_children(parent_session) == []


async def test_spawn_enforces_concurrency_cap(isolated_home, parent_session):
    for i in range(sched.SUBAGENT_MAX_CONCURRENT):
        _put_meta(_sub_meta(f"run{i}", parent="other", depth=0, status="running"))
    res = await sched.create_subagent(parent_session, "第 6 个")
    assert not res["ok"] and res.get("limit")
    assert f"Concurrent subagent cap reached ({sched.SUBAGENT_MAX_CONCURRENT})" in res["error"]
    assert "do not retry immediately" in res["error"]
    # the running list travels with the error (contract 5)
    for i in range(sched.SUBAGENT_MAX_CONCURRENT):
        assert f"run{i}" in res["error"]
    assert server_shared.subagent_children(parent_session) == []


# --------------------------------------------------------------------------- #
# completion gate (contract 6)
# --------------------------------------------------------------------------- #
async def test_gate_turn_end_with_live_child_is_waiting_not_done(
    isolated_home, capture
):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="running"))
    _put_meta(_sub_meta("G", parent="C", depth=1, status="running"))

    await sched.on_turn_settled("C")

    meta, _ = sched._find_meta("C")
    assert meta["subagent"]["status"] == "waiting"
    # no summary, no upward injection while a descendant still runs
    assert capture["wakes"] == []
    done = [
        d for _, e, d in capture["sent"] if e == "subagent.status" and d.get("status") == "done"
    ]
    assert done == []
    waiting = [
        d for _, e, d in capture["sent"] if e == "subagent.status" and d.get("status") == "waiting"
    ]
    assert waiting and waiting[0]["session_id"] == "C"


async def test_gate_last_descendant_done_finalizes_the_whole_chain(
    isolated_home, capture
):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="waiting"))
    _put_meta(_sub_meta("G", parent="C", depth=1, status="running"))

    # the last descendant finishes: done + its result wakes the waiting parent
    await sched.on_turn_settled("G")
    await _ticks()  # let the spawn_bg wake task run
    gmeta, _ = sched._find_meta("G")
    assert gmeta["subagent"]["status"] == "done"
    assert gmeta["subagent"]["result_summary"]  # fallback summary, still set
    assert len(capture["wakes"]) == 1
    pid, text, extra = capture["wakes"][0]
    assert pid == "C"
    assert '<ginno_subagent_result session="G"' in text
    assert extra == {"ginno_subagent_result": "G"}

    # the woken parent finishes its wrap-up turn: now it is really done and
    # ITS result flows up to the main conversation
    await sched.on_turn_settled("C")
    await _ticks()
    cmeta, _ = sched._find_meta("C")
    assert cmeta["subagent"]["status"] == "done"
    assert len(capture["wakes"]) == 2
    assert capture["wakes"][1][0] == "P"
    assert '<ginno_subagent_result session="C"' in capture["wakes"][1][1]


async def test_result_injection_uses_steer_when_parent_running(isolated_home):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="running"))
    server_shared._RUNNING_TURNS["P"] = "turn-p1"
    try:
        await sched.on_turn_settled("C")
        stash = server_shared._STEER_STASH.get("P") or []
        assert len(stash) == 1
        msgs = server_shared.steer_messages(stash)
        # contract 3: the persisted HumanMessage carries both the steer marker
        # and the ginno_subagent_result tag with the child id
        m = msgs[0]
        assert '<ginno_subagent_result session="C"' in str(m.content)
        assert m.additional_kwargs["ginno_subagent_result"] == "C"
        assert m.additional_kwargs["ginno_steer"]["steer_id"].startswith("subagent-C-")
    finally:
        server_shared._RUNNING_TURNS.pop("P", None)


async def test_user_stopped_subagent_is_stopped_and_injects_nothing(isolated_home):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="running"))
    evt = server_shared._TURN_STOP.setdefault("C", asyncio.Event())
    evt.set()
    await sched.on_turn_settled("C", evt, "t1")
    meta, _ = sched._find_meta("C")
    assert meta["subagent"]["status"] == "stopped"
    # user kill: nothing reaches the parent (design §5.7)
    assert server_shared._STEER_STASH.get("P") is None


async def test_failed_turn_reports_failure_without_summary(isolated_home, capture):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="running"))
    # a persisted failure for this turn (what AUTO_RETRY exhaustion leaves)
    _session_meta_upsert(
        SLUG,
        {
            **_sub_meta("C", parent="P", depth=0, status="running"),
            "last_error": {"turn_id": "t-fail", "message": "Boom: 500", "at": time.time()},
        },
    )
    await sched.on_turn_settled("C", None, "t-fail")
    meta, _ = sched._find_meta("C")
    assert meta["subagent"]["status"] == "failed"
    status = [
        d
        for _, e, d in capture["sent"]
        if e == "subagent.status" and d.get("session_id") == "C"
    ]
    assert status and status[0]["status"] == "failed" and "Boom" in status[0]["error"]
    # failure notice rides the wake channel (P idle), clearly NOT a summary
    await _ticks()
    assert capture["wakes"] and "Status: failed" in capture["wakes"][0][1]


async def test_all_descendants_stopped_wakes_waiting_parent(isolated_home, capture):
    _put_meta(_sub_meta("C2", parent="P2", depth=0, status="waiting"))
    _put_meta(_sub_meta("G2", parent="C2", depth=1, status="stopped"))

    await sched._reevaluate_waiting_parent("C2")
    await _ticks()
    # design §5.6 anti-starvation: the waiting parent gets one wrap-up wake
    assert capture["wakes"] and capture["wakes"][0][0] == "C2"
    assert "All of your subtasks have been stopped by the user" in capture["wakes"][0][1]


# --------------------------------------------------------------------------- #
# stop cascade + delete cascade (contract 7 / §5.8)
# --------------------------------------------------------------------------- #
async def test_stop_cascade_sets_events_down_the_tree(isolated_home):
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("C", parent="P", depth=0, status="running"))
    _put_meta(_sub_meta("G", parent="C", depth=1, status="running"))
    _put_meta(_sub_meta("GG", parent="G", depth=2, status="waiting"))
    ec = server_shared._TURN_STOP.setdefault("C", asyncio.Event())
    eg = server_shared._TURN_STOP.setdefault("G", asyncio.Event())

    stopped = await sched.stop_subagent_tree("C", include_self=True)
    # GG is waiting = no live turn of its own: nothing to SIGNAL there (it is
    # finalized "stopped" instead — see the major-3 regression test below)
    assert set(stopped) == {"C", "G"}
    assert ec.is_set() and eg.is_set()
    # descendants-only variant leaves the session itself alone
    eg.clear()
    assert await sched.stop_descendants("C") == ["G"]


async def test_delete_cascade_removes_descendants_and_checkpoints(isolated_home):
    _put_meta(_sub_meta("DP"))
    _put_meta(_sub_meta("DC", parent="DP", depth=0, status="done"))
    _put_meta(_sub_meta("DG", parent="DC", depth=1, status="stopped"))
    sessions_dir = paths.project_sessions_dir(SLUG)
    for sid in ("DP", "DC", "DG"):
        (sessions_dir / f"{sid}.json").write_text("{}", encoding="utf-8")

    removed = await sched.delete_subagent_tree(SLUG, "DP")
    assert set(removed) == {"DC", "DG"}
    ids = [m["id"] for m in _session_meta_list(SLUG)]
    assert "DP" in ids and "DC" not in ids and "DG" not in ids
    assert not (sessions_dir / "DC.json").exists()
    assert not (sessions_dir / "DG.json").exists()
    # the deleted session itself is untouched (the caller removes it)
    assert (sessions_dir / "DP.json").exists()
    assert server_shared.subagent_children("DP") == []
    assert server_shared.subagent_children("DC") == []


# --------------------------------------------------------------------------- #
# listing + wait (contract 4)
# --------------------------------------------------------------------------- #
async def test_list_rows_cover_direct_and_descendants(isolated_home):
    _put_meta(_sub_meta("L0"))
    _put_meta(_sub_meta("L1", parent="L0", depth=0, status="running"))
    _put_meta(_sub_meta("L2", parent="L1", depth=1, status="done"))
    rows = sched.collect_subagent_rows(SLUG, "L0")
    assert [r["session_id"] for r in rows] == ["L1", "L2"]
    assert rows[0]["status"] == "running" and rows[0]["depth"] == 0
    assert rows[1]["parent_session_id"] == "L1" and rows[1]["elapsed_s"] >= 0


async def test_wait_returns_when_all_terminal(isolated_home):
    _put_meta(_sub_meta("WP"))
    done_meta = _sub_meta("WD", parent="WP", depth=0, status="done")
    done_meta["subagent"]["result_summary"] = "结论 A"
    _put_meta(done_meta)
    res = await sched.wait_for_subagents("WP", None, 0)
    data = json.loads(res)
    assert data["partial"] is False
    assert data["subagents"][0]["session_id"] == "WD"
    assert data["subagents"][0]["status"] == "done"
    assert data["subagents"][0]["result_summary"] == "结论 A"


async def test_wait_unknown_ids_error_instead_of_hanging(isolated_home):
    _put_meta(_sub_meta("WP2"))
    res = await sched.wait_for_subagents("WP2", ["nope"], 0)
    assert res.startswith("[error]") and "nope" in res


async def test_wait_timeout_returns_partial(isolated_home):
    _put_meta(_sub_meta("WP3"))
    _put_meta(_sub_meta("WR", parent="WP3", depth=0, status="running"))
    res = await sched.wait_for_subagents("WP3", None, 1)
    data = json.loads(res[res.index("{"):])
    assert data["partial"] is True
    assert data["subagents"][0]["status"] == "running"


async def test_wait_interrupts_on_user_stop(isolated_home):
    _put_meta(_sub_meta("WP4"))
    _put_meta(_sub_meta("WR4", parent="WP4", depth=0, status="running"))
    server_shared._TURN_STOP.setdefault("WP4", asyncio.Event()).set()
    res = await sched.wait_for_subagents("WP4", None, 0)
    assert res.startswith("[stopped]")


# --------------------------------------------------------------------------- #
# toolset registration (contract 4) + message format (contract 3)
# --------------------------------------------------------------------------- #
def test_toolset_depth_gating(isolated_home):
    from ginno_runtime.graph import build_all_tools

    def names(**kw):
        return {t.name for t in build_all_tools(**kw)}

    main = names(session_id="s1")
    assert {"spawn_subagent", "list_subagents"} <= main
    assert "wait_subagents" not in main  # folded into list_subagents(wait=true)
    d0 = names(session_id="s1", subagent_depth=0)
    assert "spawn_subagent" in d0
    d2 = names(session_id="s1", subagent_depth=2)
    # structural cap: no spawn at depth 2, but the read/wait tool remains
    assert "spawn_subagent" not in d2
    assert "list_subagents" in d2
    # workflow engine / listing endpoints get none
    assert not {"spawn_subagent", "list_subagents"} & names()


def test_result_message_format_contract():
    text = sched.format_subagent_result("kid", "调研", "结论：选 A")
    assert text.startswith('<ginno_subagent_result session="kid" goal="调研">')
    assert "\n结论：选 A\n" in text
    assert text.endswith("</ginno_subagent_result>")
    fail = sched.format_subagent_failure("kid", "调研", "Boom: 500")
    assert 'session="kid"' in fail and "Boom: 500" in fail and "Status: failed" in fail


def test_subagent_depth_of_meta():
    assert subagent_depth_of({"type": "subagent", "depth": 2}) == 2
    assert subagent_depth_of({"type": "quick"}) == -1
    assert subagent_depth_of(None) == -1
    assert subagent_depth_of({"type": "subagent"}) == -1


# --------------------------------------------------------------------------- #
# review-fix regressions (major-1..5, minor-6..9)
# --------------------------------------------------------------------------- #
def _stash_injection(parent: str, child: str) -> None:
    server_shared.steer_enqueue(
        parent,
        {
            "steer_id": f"subagent-{child}-abcd1234",
            "turn_id": "turn-p",
            "text": (
                f'<ginno_subagent_result session="{child}" goal="g">\n结论\n'
                "</ginno_subagent_result>"
            ),
            "injected_at": time.time(),
            "extra_kwargs": {"ginno_subagent_result": child},
        },
    )


async def test_reconcile_redelivers_unabsorbed_subagent_injection(
    isolated_home, capture
):
    """major-1: a result injection that missed the parent's last drain must be
    redelivered through the wake channel, not dropped by steer_clear (the
    scheduler never re-sends, unlike the frontend's user-steer resend)."""
    _stash_injection("RP", "RC")
    server_shared.steer_enqueue(
        "RP",
        {
            "steer_id": "user-1",
            "turn_id": "turn-p",
            "text": "用户补充",
            "injected_at": time.time(),
        },
    )
    n = sched.reconcile_stash_injections("RP")
    assert n == 1
    await _ticks()  # let the spawn_bg wake task run
    assert len(capture["wakes"]) == 1
    pid, text, extra = capture["wakes"][0]
    assert pid == "RP" and 'session="RC"' in text
    assert extra == {"ginno_subagent_result": "RC"}
    # the USER steer is untouched: it keeps the frontend's own resend semantics
    stash = server_shared._STEER_STASH.get("RP") or []
    assert [e["steer_id"] for e in stash] == ["user-1"]


async def test_settle_hook_reconciles_restashed_injection(isolated_home, capture):
    """major-1, raced-wake shape: an injection a busy wake re-stashed is caught
    by the settle-side reconciliation instead of sitting in the stash forever."""
    _stash_injection("SP", "SC")
    await sched.on_turn_settled("SP")  # main conversation — only reconcile runs
    await _ticks()
    assert capture["wakes"] and capture["wakes"][0][0] == "SP"
    assert server_shared._STEER_STASH.get("SP") is None


async def test_parked_stop_reconciles_before_steer_clear(
    monkeypatch, isolated_home, capture
):
    """major-1, parked shape: stopping a turn parked at a permission card must
    redeliver tagged injections before the generic stash drop."""
    redelivered: list[str] = []

    real_reconcile = sched.reconcile_stash_injections

    def spy(sid: str) -> int:
        n = real_reconcile(sid)
        if n:
            redelivered.append(sid)
        return n

    monkeypatch.setattr(sched, "reconcile_stash_injections", spy)
    # capture.stream is stream.py's module reference — _stop_parked_turn calls
    # the scheduler's reconciler through its own lazy import, which this spy
    # replaces at the source module.
    _stash_injection("PP", "PC")
    await stream_mod._stop_parked_turn(
        {"graph": None, "project_slug": SLUG}, "PP", "turn-pp"
    )
    assert redelivered == ["PP"]
    await _ticks()
    assert capture["wakes"] and capture["wakes"][0][0] == "PP"


async def test_mixed_terminal_descendants_wake_waiting_parent(isolated_home, capture):
    """major-2: C1 done + C2 stopped still leaves no one to wake the parent —
    the old all-stopped guard stranded it ⏳ forever."""
    _put_meta(_sub_meta("MP", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("MD", parent="MP", depth=0, status="done"))
    _put_meta(_sub_meta("MS", parent="MP", depth=0, status="stopped"))

    await sched._reevaluate_waiting_parent("MP")
    await _ticks()
    assert capture["wakes"] and capture["wakes"][0][0] == "MP"
    assert "were stopped by the user" in capture["wakes"][0][1]


async def test_all_done_descendants_do_not_double_wake(isolated_home, capture):
    """major-2 flip side: when every descendant finished with its own result
    wake, the anti-starvation path must stay quiet (no duplicate injection)."""
    _put_meta(_sub_meta("NP", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("ND1", parent="NP", depth=0, status="done"))
    _put_meta(_sub_meta("ND2", parent="NP", depth=0, status="failed"))
    await sched._reevaluate_waiting_parent("NP")
    await _ticks()
    assert capture["wakes"] == []


async def test_result_not_injected_into_terminal_parent(isolated_home, capture):
    """major-3: a child finishing after its parent was cascade-stopped must not
    wake the dead parent (§5.6: 被级联停止的不向已停止的父注入结果)."""
    _put_meta(_sub_meta("TP", parent="ROOT", depth=1, status="stopped"))
    _put_meta(_sub_meta("TC", parent="TP", depth=1, status="running"))

    await sched.on_turn_settled("TC")
    await _ticks()
    meta, _ = sched._find_meta("TC")
    assert meta["subagent"]["status"] == "done"  # the child itself finalizes
    assert capture["wakes"] == []
    assert server_shared._STEER_STASH.get("TP") is None


async def test_cascade_finalizes_waiting_descendant(isolated_home, capture):
    """major-3: A(running) → B(waiting) → C(running): stopping A settles B as
    stopped directly — B has no turn to signal, and leaving it waiting would
    let the anti-starvation wake revive work under a dead parent."""
    _put_meta(_sub_meta("A3"))
    _put_meta(_sub_meta("B3", parent="A3", depth=0, status="waiting"))
    _put_meta(_sub_meta("C3", parent="B3", depth=1, status="running"))
    eg = server_shared._TURN_STOP.setdefault("C3", asyncio.Event())

    await sched.stop_subagent_tree("A3", include_self=True)
    bmeta, _ = sched._find_meta("B3")
    assert bmeta["subagent"]["status"] == "stopped"
    assert eg.is_set()  # C keeps the cooperative path (its turn settles it)


async def test_cascade_heals_parked_descendant(monkeypatch, isolated_home, capture):
    """major-4: a parked (permission-card) descendant is healed + finalized via
    the same _stop_parked_turn path, and its fake running markers are cleared
    so the delete busy-wait cannot spin on them."""
    parked: list[tuple[str, str]] = []

    async def fake_stop_parked(session, sid, tid):
        parked.append((sid, tid))

    monkeypatch.setattr(
        "ginno_runtime.api.stream._stop_parked_turn", fake_stop_parked
    )
    _put_meta(_sub_meta("A4"))
    _put_meta(_sub_meta("D4", parent="A4", depth=0, status="running"))
    server_shared._SESSIONS["D4"] = {"session_id": "D4", "project_slug": SLUG}
    server_shared._PENDING_RESUME.add("D4")
    server_shared._PENDING_KIND["D4"] = "permission_request"
    server_shared._RUNNING_TURNS["D4"] = "turn-d4"

    stopped = await sched.stop_subagent_tree("A4", include_self=True)
    assert "D4" in stopped
    assert parked == [("D4", "turn-d4")]
    assert "D4" not in server_shared._PENDING_RESUME
    assert "D4" not in server_shared._RUNNING_TURNS


async def test_delete_cascade_with_parked_descendant_not_blocked(
    monkeypatch, isolated_home
):
    """major-4: the delete busy-wait must not count a parked session's resident
    _RUNNING_TURNS entry as busy (it used to idle the full 10s cap)."""
    async def fake_stop_parked(session, sid, tid):
        return None

    monkeypatch.setattr(
        "ginno_runtime.api.stream._stop_parked_turn", fake_stop_parked
    )
    _put_meta(_sub_meta("EP"))
    _put_meta(_sub_meta("EQ", parent="EP", depth=0, status="running"))
    server_shared._SESSIONS["EQ"] = {"session_id": "EQ", "project_slug": SLUG}
    server_shared._PENDING_RESUME.add("EQ")
    server_shared._RUNNING_TURNS["EQ"] = ""

    started = time.monotonic()
    removed = await sched.delete_subagent_tree(SLUG, "EP")
    assert time.monotonic() - started < 5.0
    assert removed == ["EQ"]
    assert "EQ" not in server_shared._RUNNING_TURNS


async def test_delete_live_child_wakes_waiting_parent(isolated_home, capture):
    """major-5: deleting a waiting parent's last live child settles the child
    BEFORE its meta disappears, so the waiting ancestor is re-evaluated."""
    _put_meta(_sub_meta("WP5", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("WC5", parent="WP5", depth=0, status="running"))

    await sched.finalize_for_delete("WC5")
    meta, _ = sched._find_meta("WC5")
    assert meta["subagent"]["status"] == "stopped"
    await _ticks()
    assert capture["wakes"] and capture["wakes"][0][0] == "WP5"


async def test_delete_waiting_parent_reevaluates_its_ancestor(
    isolated_home, capture
):
    """major-5, ancestor shape: removing a waiting middle layer wakes ITS
    waiting parent up the chain (the settle hook can no longer run for either
    removed meta)."""
    _put_meta(_sub_meta("GP6", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("W6", parent="GP6", depth=1, status="waiting"))
    _put_meta(_sub_meta("K6", parent="W6", depth=2, status="done"))

    await sched.finalize_for_delete("W6")
    await _ticks()
    wmeta, _ = sched._find_meta("W6")
    assert wmeta["subagent"]["status"] == "stopped"
    assert capture["wakes"] and capture["wakes"][0][0] == "GP6"


async def test_retry_revives_failed_subagent(isolated_home, capture):
    """minor-6: a failed subagent's meta is terminal, so the retry's settle
    gate would no-op — revive flips it back to running first."""
    _put_meta(_sub_meta("P7"))
    _put_meta(_sub_meta("C7", parent="P7", depth=0, status="failed"))

    await sched.revive_failed_for_retry("C7")
    meta, _ = sched._find_meta("C7")
    assert meta["subagent"]["status"] == "running"
    running = [
        d
        for _, e, d in capture["sent"]
        if e == "subagent.status" and d.get("status") == "running"
    ]
    assert running and running[0]["session_id"] == "C7"
    # other terminal states and non-subagents stay untouched
    _put_meta(_sub_meta("C7b", parent="P7", depth=0, status="done"))
    await sched.revive_failed_for_retry("C7b")
    meta, _ = sched._find_meta("C7b")
    assert meta["subagent"]["status"] == "done"
    await sched.revive_failed_for_retry("P7")
    meta, _ = sched._find_meta("C7b")
    assert meta["subagent"]["status"] == "done"


async def test_wake_turn_usage_source_follows_session_kind(monkeypatch, isolated_home):
    """minor-7: the wake channel also serves MAIN conversations — only a
    subagent session's usage is tagged ``subagent``."""
    seen: list[str] = []

    async def fake_run_stream(ws, graph, config, text, session, agent_id, user_extra_kwargs=None):
        seen.append(config["configurable"]["usage_source"])

    async def fake_settled(session_id, stop_evt=None, turn_id=""):
        return None

    monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
    monkeypatch.setattr(sched, "on_turn_settled", fake_settled)
    server_shared._SESSIONS["M8"] = {
        "session_id": "M8",
        "project_slug": SLUG,
        "workspace": "/tmp",
        "agent_id": "dev",
        "type": None,
        "graph": object(),
    }
    await sched._run_managed_turn_locked("M8", "注入", None, asyncio.Event())
    assert seen == ["chat"]

    _put_meta(_sub_meta("S8", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["S8"] = {
        "session_id": "S8",
        "project_slug": SLUG,
        "workspace": "/tmp",
        "agent_id": "dev",
        "type": "subagent",
        "graph": object(),
    }
    await sched._run_managed_turn_locked("S8", "注入", None, asyncio.Event())
    assert seen == ["chat", "subagent"]


async def test_status_broadcast_reaches_all_ancestors(isolated_home, capture):
    """minor-8: subagent.* frames ride the whole parent chain, so a
    grandparent's sidebar never shows a stale nested status dot."""
    _put_meta(_sub_meta("R9"))  # main conversation (chain root)
    _put_meta(_sub_meta("M9", parent="R9", depth=0, status="running"))
    _put_meta(_sub_meta("L9", parent="M9", depth=1, status="running"))

    await sched._push_both("M9", "L9", "subagent.status", {"status": "done"})
    targets = {sid for sid, _e, _d in capture["sent"]}
    assert targets == {"R9", "M9", "L9"}


async def test_concurrency_cap_ignores_waiting(isolated_home, parent_session, capture):
    """minor-9a: the hard cap counts in-flight TURNS — a waiting parent holds
    no turn of its own and must not consume a slot. 4 running + 1 waiting = a
    new spawn still fits (5 running would be the cap, covered above)."""
    for i in range(sched.SUBAGENT_MAX_CONCURRENT - 1):
        _put_meta(_sub_meta(f"cr{i}", parent="other", depth=0, status="running"))
    _put_meta(_sub_meta("cw", parent="other", depth=0, status="waiting"))
    res = await sched.create_subagent(parent_session, "补一个")
    assert res["ok"] and res["depth"] == 0


def test_result_format_escapes_goal_attributes():
    """minor-9b: a model-authored goal containing quotes must not break the
    attribute envelope the frontend's parseSubagentResult matches on."""
    text = sched.format_subagent_result("kid", '目标含"引号"与<&>', "结论")
    header = text.split(">", 1)[0]
    assert "&quot;" in header and "&lt;" in header and "&amp;" in header
    assert header.count('"') == 4  # exactly session="…" + goal="…"
    fail = sched.format_subagent_failure("kid", 'go"al', "Boom")
    assert fail.split(">", 1)[0].count('"') == 4


# --------------------------------------------------------------------------- #
# adversarial-review round 2 (major-A / major-B / minor-C): check-then-act
# across await boundaries. These drive the REAL scheduler paths —
# _wake_parent_turn included — with only the stream layer faked.
# --------------------------------------------------------------------------- #
def _sess(sid: str, graph: Any = None, type_: str | None = None) -> dict:
    """Minimal in-memory session entry (what the scheduler actually reads)."""
    return {
        "session_id": sid,
        "project_slug": SLUG,
        "workspace": "/tmp",
        "agent_id": "dev",
        "model_provider": "custom",
        "model_name": None,
        "type": type_,
        "graph": graph if graph is not None else object(),
        "context_folders": [],
        "primary_folder": None,
    }


@pytest.fixture
def radio(monkeypatch):
    """Fakes the WS push edge in BOTH modules the fixes touch (the scheduler's
    _push_both and stream.py's stop path), recording every frame."""
    state: dict = {"sent": []}

    async def fake_push(session_id, event, data, turn_id=None):
        state["sent"].append((session_id, event, data))

    monkeypatch.setattr(sched, "_push_session_event", fake_push)
    monkeypatch.setattr(stream_mod, "_push_session_event", fake_push)
    return state


@pytest.fixture
def fake_stream(monkeypatch, radio):
    """Light fake of the turn engine for scheduler-managed turns: every real
    _run_managed_turn / _wake_parent_turn call records (session, text) and
    returns immediately. The scheduler's wake/settle/finalize logic all runs
    for real — nothing above _run_stream is mocked."""
    state: dict = {"streams": [], "sent": radio["sent"]}

    async def fake_run_stream(ws, graph, config, text, session, agent_id, user_extra_kwargs=None, **kw):
        state["streams"].append((config["configurable"]["thread_id"], text))

    monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
    return state


async def _drain(state: dict, timeout_s: float = 5.0) -> None:
    """Wait until the spawned-task chain goes quiet (two identical snapshots)."""
    seen = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        snap = (len(state["streams"]), len(state["sent"]))
        if snap == seen:
            return
        seen = snap


async def _ticks(n: int = 6) -> None:
    """Pump the loop a few turns — the merged-injection queue added an async
    hop (queue -> flush -> wake), so a single sleep(0) no longer reaches the
    wake in tests."""
    for _ in range(n):
        await asyncio.sleep(0)


def _stream_count(state: dict, sid: str) -> int:
    return sum(1 for s, _t in state["streams"] if s == sid)


def _status_events(state: dict, target: str, sid: str, status: str) -> list:
    """subagent.status frames DELIVERED TO ``target`` about session ``sid``.
    (_push_both broadcasts one finalize to every chain member, so counting
    frames without a target would double-count each finalize.)"""
    return [
        d
        for tgt, e, d in state["sent"]
        if tgt == target and e == "subagent.status"
        and d.get("session_id") == sid and d.get("status") == status
    ]


class _StopFirstGraph:
    """Graph stand-in for the REAL _stream_graph: the first segment has the
    user's stop land mid-turn (the WS handler sets the cooperative event), so
    the TurnStopped unwind + finally bookkeeping run for real; any later turn
    (a ghost) completes normally with no output."""

    def __init__(self, sid: str):
        self.sid = sid
        self.calls = 0

    def astream(self, input_state, config=None, stream_mode=None):
        graph = self

        async def gen():
            graph.calls += 1
            if graph.calls == 1:
                evt = server_shared._TURN_STOP.get(graph.sid)
                if evt is not None:
                    evt.set()
            return
            yield None  # pragma: no cover

        return gen()


async def test_double_wake_runs_one_turn_and_injects_once(isolated_home, fake_stream):
    """major-A: two children of one waiting parent finalize back-to-back; each
    _inject_result judges the parent idle BEFORE the other's wake flips any
    state, so two real _wake_parent_turn tasks race for the turn lock. The
    under-lock re-read must drop the second: one turn, one done, ONE upward
    injection into the grandparent (pre-fix: two turns, done twice, ROOT
    injected twice)."""
    _put_meta(_sub_meta("ROOT"))  # main conversation
    _put_meta(_sub_meta("PA", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("CA", parent="PA", depth=0, status="done"))
    _put_meta(_sub_meta("CB", parent="PA", depth=0, status="done"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["PA"] = _sess("PA", type_="subagent")

    # Both finalizers saw PA idle (neither wake has run yet): two wake tasks
    # now contend for PA's turn lock, exactly like the production race.
    await sched._inject_result(
        "PA", sched.format_subagent_result("CA", "g", "结论 A"), "CA"
    )
    await sched._inject_result(
        "PA", sched.format_subagent_result("CB", "g", "结论 B"), "CB"
    )
    await _drain(fake_stream)

    pa, _ = sched._find_meta("PA")
    assert pa["subagent"]["status"] == "done"
    assert _stream_count(fake_stream, "PA") == 1  # no ghost second turn
    assert _stream_count(fake_stream, "ROOT") == 1  # injected exactly once
    assert len(_status_events(fake_stream, "ROOT", "PA", "done")) == 1


async def test_stopped_turn_is_not_ghosted_by_reconcile_wake(
    monkeypatch, isolated_home, fake_stream
):
    """major-B (live shape): the user stops PG's turn while an unabsorbed child
    result sits in the stash. The reconcile in _stream_graph's finally spawns a
    redelivery wake the moment the lock frees — it must not win the lock ahead
    of the stopped finalize and open a ghost turn (pre-fix: the settle hook
    bounced off the _RUNNING_TURNS guard, the stopped finalize never ran, and
    the ghost's settle marked the stopped session done + injected it)."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PG", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    graph = _StopFirstGraph("PG")
    server_shared._SESSIONS["PG"] = _sess("PG", graph=graph, type_="subagent")
    _stash_injection("PG", "CG")

    # Real _stream_graph (its finally hosts the reconcile + the fix); only the
    # prompt-building _run_stream wrapper is skipped.
    async def passthrough(ws, g, config, text, session, agent_id, user_extra_kwargs=None, **kw):
        fake_stream["streams"].append((config["configurable"]["thread_id"], text))
        await stream_mod._stream_graph(g, config, input_state=None)

    monkeypatch.setattr(stream_mod, "_run_stream", passthrough)

    # WS-invoke shape (the production path): the turn runs as a _TURN_TASKS-
    # registered job and its settle hook is a SEPARATE spawned task (as
    # _turn_job does) — so the reconcile's wake, spawned earlier inside the
    # dying segment, is scheduled AHEAD of the settle and can win the freed
    # lock first. That ordering is what let the ghost through pre-fix.
    async def job():
        evt = server_shared._TURN_STOP.setdefault("PG", asyncio.Event())
        tid = "turn-pg"
        server_shared._RUNNING_TURNS["PG"] = tid
        server_shared._TURN_TASKS["PG"] = asyncio.current_task()
        try:
            async with server_shared._turn_lock("PG"):
                await stream_mod._run_stream(
                    None, graph, {"configurable": {
                        "thread_id": "PG", "project_slug": SLUG,
                        "turn_id": tid, "agent_id": "dev",
                    }},
                    "继续", server_shared._SESSIONS["PG"], "dev",
                )
        finally:
            if server_shared._TURN_TASKS.get("PG") is asyncio.current_task():
                server_shared._TURN_TASKS.pop("PG", None)
            server_shared._TURN_STOP.pop("PG", None)
            if server_shared._RUNNING_TURNS.get("PG") == tid:
                server_shared._RUNNING_TURNS.pop("PG", None)
            spawn_bg(sched.on_turn_settled("PG", evt, tid))

    await job()
    await _drain(fake_stream)

    meta, _ = sched._find_meta("PG")
    assert meta["subagent"]["status"] == "stopped"
    assert _stream_count(fake_stream, "PG") == 1  # the stopped segment only
    assert _stream_count(fake_stream, "ROOT") == 0  # stopped injects nothing
    assert _status_events(fake_stream, "ROOT", "PG", "done") == []


async def test_parked_stop_finalizes_before_reconcile(isolated_home, fake_stream):
    """major-B (parked shape): stopping a subagent parked at a permission card
    must finalize "stopped" BEFORE the reconcile's redelivery wake runs — the
    wake would otherwise revive it for a full ghost turn (pre-fix order:
    reconcile → wake → ghost → done)."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PE", parent="ROOT", depth=0, status="running"))
    _put_meta(_sub_meta("CE", parent="PE", depth=0, status="done"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["PE"] = _sess("PE", type_="subagent")
    _stash_injection("PE", "CE")  # unabsorbed child result in the stash

    await stream_mod._stop_parked_turn(
        {"graph": None, "project_slug": SLUG}, "PE", "turn-pe"
    )
    await _drain(fake_stream)

    meta, _ = sched._find_meta("PE")
    assert meta["subagent"]["status"] == "stopped"
    assert _stream_count(fake_stream, "PE") == 0  # no ghost turn
    assert _stream_count(fake_stream, "ROOT") == 0  # stopped injects nothing


async def test_stop_in_settle_window_does_not_mark_success_stopped(
    isolated_home, fake_stream
):
    """minor-C: the stop lands after the turn finished normally but before the
    settle hook ran (_TURN_TASKS/_RUNNING_TURNS already popped, meta still
    "running" from the wake flip) — the just-succeeded turn must settle done
    with its upward injection, not be mislabeled stopped."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PB", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")

    await sched.on_stop_if_waiting("PB")  # must refuse: PB is not WAITING
    meta, _ = sched._find_meta("PB")
    assert meta["subagent"]["status"] == "running"

    await sched.on_turn_settled("PB", None, "t-ok")
    await _drain(fake_stream)
    meta, _ = sched._find_meta("PB")
    assert meta["subagent"]["status"] == "done"
    assert _stream_count(fake_stream, "ROOT") == 1  # the result reached the parent


async def test_stop_if_waiting_refuses_live_turn_even_when_waiting(isolated_home):
    """minor-C guard: a waiting meta with a LIVE turn (wake granted, still
    running) is not finalized by the parked-stop path — the turn's own
    machinery owns that settlement."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PD", parent="ROOT", depth=0, status="waiting"))
    server_shared._RUNNING_TURNS["PD"] = "turn-pd"
    try:
        await sched.on_stop_if_waiting("PD")
        meta, _ = sched._find_meta("PD")
        assert meta["subagent"]["status"] == "waiting"  # untouched
    finally:
        server_shared._RUNNING_TURNS.pop("PD", None)


async def test_stop_if_waiting_still_finalizes_parked_waiting(isolated_home, radio):
    """minor-C flip side: a genuinely parked WAITING subagent (no turn of its
    own) is still finalized stopped, and injects nothing upward (§5.7)."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PF", parent="ROOT", depth=0, status="waiting"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")

    await sched.on_stop_if_waiting("PF")
    meta, _ = sched._find_meta("PF")
    assert meta["subagent"]["status"] == "stopped"
    assert server_shared._STEER_STASH.get("ROOT") is None


# --------------------------------------------------------------------------- #
# adversarial-review round 3 (封底验证): fresh attacks on the SAME three fixes.
# --------------------------------------------------------------------------- #
@pytest.fixture
def yielding_push(monkeypatch):
    """Like ``radio`` but the push actually SUSPENDS, so the flip window inside
    _wake_parent_turn (waiting→running broadcast) becomes interleavable — the
    plain recorder fakes return without yielding and hide that window."""
    state: dict = {"sent": []}
    hooks: list = []

    async def push(session_id, event, data, turn_id=None):
        state["sent"].append((session_id, event, data))
        for hook in list(hooks):
            await hook(session_id, event, data)
        await _ticks()  # the real WS send always suspends

    monkeypatch.setattr(sched, "_push_session_event", push)
    monkeypatch.setattr(stream_mod, "_push_session_event", push)
    return state, hooks


async def test_triple_wake_single_batch_finalizes_once(isolated_home, fake_stream):
    """major-A, three-child batch: unlike the fixer's two-child test, THREE
    finalizers spawn wakes before any of them flips the parent. All three
    contend for the lock; the under-lock re-read must drop #2 and #3 alike —
    one turn, one done broadcast, ONE injection into ROOT."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PX", parent="ROOT", depth=0, status="waiting"))
    for cid in ("CX1", "CX2", "CX3"):
        _put_meta(_sub_meta(cid, parent="PX", depth=0, status="done"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["PX"] = _sess("PX", type_="subagent")

    for cid in ("CX1", "CX2", "CX3"):
        await sched._inject_result(
            "PX", sched.format_subagent_result(cid, "g", f"结论 {cid}"), cid
        )
    await _drain(fake_stream)

    px, _ = sched._find_meta("PX")
    assert px["subagent"]["status"] == "done"
    assert _stream_count(fake_stream, "PX") == 1  # exactly one wrap-up turn
    assert _stream_count(fake_stream, "ROOT") == 1  # injected exactly once
    assert len(_status_events(fake_stream, "ROOT", "PX", "done")) == 1
    # the refused wakes must not leave a stranded busy marker behind
    assert "PX" not in server_shared._RUNNING_TURNS
    assert "PX" not in server_shared._TURN_STOP


async def test_stop_during_parked_resume_segment_not_ghosted(
    monkeypatch, isolated_home, fake_stream
):
    """major-B, parked-resume shape: PR parked at a permission card with an
    unabsorbed child result in the stash (kept by the park); the user ANSWERS,
    the resumed segment runs (production _resume_job shape: lock + _run_resume
    + spawn_bg settle), and the user presses stop mid-resume. The resumed
    segment's reconcile must not spawn a wake that outruns the stopped
    finalize and re-opens the session."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PR", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    graph = _StopFirstGraph("PR")
    server_shared._SESSIONS["PR"] = _sess("PR", graph=graph, type_="subagent")
    _stash_injection("PR", "CR")  # survived the park (else-branch keeps it)

    async def passthrough(ws, g, config, text, session, agent_id, user_extra_kwargs=None, **kw):
        fake_stream["streams"].append((config["configurable"]["thread_id"], text))
        await stream_mod._stream_graph(g, config, input_state=None)

    # Production resume does NOT go through _run_stream: the WS resume handler
    # calls _run_resume (→ _stream_graph with command) directly.
    async def passthrough_resume(ws, g, config, resume_value):
        fake_stream["streams"].append(
            (config["configurable"]["thread_id"], "<resume>")
        )
        from langgraph.types import Command

        await stream_mod._stream_graph(g, config, command=Command(resume=resume_value))

    monkeypatch.setattr(stream_mod, "_run_stream", passthrough)
    monkeypatch.setattr(stream_mod, "_run_resume", passthrough_resume)

    # _resume_job shape (permission_response already discarded _PENDING_RESUME
    # before _spawn_resume — the resumed segment runs un-parked).
    async def resume_job():
        evt = server_shared._TURN_STOP.setdefault("PR", asyncio.Event())
        tid = "turn-pr-resume"
        server_shared._RUNNING_TURNS["PR"] = tid
        server_shared._TURN_TASKS["PR"] = asyncio.current_task()
        try:
            async with server_shared._turn_lock("PR"):
                await stream_mod._run_resume(
                    None,
                    graph,
                    {"configurable": {
                        "thread_id": "PR", "project_slug": SLUG,
                        "turn_id": tid, "agent_id": "dev",
                    }},
                    {"decision": "approve"},
                )
        finally:
            if server_shared._TURN_TASKS.get("PR") is asyncio.current_task():
                server_shared._TURN_TASKS.pop("PR", None)
            server_shared._TURN_STOP.pop("PR", None)
            spawn_bg(sched.on_turn_settled("PR", evt, tid))

    await resume_job()
    await _drain(fake_stream)

    meta, _ = sched._find_meta("PR")
    assert meta["subagent"]["status"] == "stopped"
    assert _stream_count(fake_stream, "PR") == 1  # the resumed segment only
    assert _stream_count(fake_stream, "ROOT") == 0  # stopped injects nothing
    assert _status_events(fake_stream, "ROOT", "PR", "done") == []


async def test_ws_stop_in_wake_flip_window_still_stops(
    isolated_home, fake_stream, yielding_push
):
    """minor-C attack: the stop lands inside the wake's flip window — the turn
    lock is already held but _RUNNING_TURNS is NOT registered yet. The WS stop
    handler's liveness gate (replicated verbatim in the hook) then judges the
    session idle and sets NO event, and the queued on_stop_if_waiting later
    bounces off the ``waiting``-status check: the user's stop is swallowed and
    the full wake turn runs anyway."""
    _state, hooks = yielding_push
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PW", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("CW", parent="PW", depth=0, status="done"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["PW"] = _sess("PW", type_="subagent")

    pressed = False

    async def ws_stop_like_handler(sid, event, data):
        # Verbatim replica of the stop branch's liveness gate + event set.
        nonlocal pressed
        if sid == "PW" and event == "subagent.status" and not pressed:
            pressed = True
            t = server_shared._TURN_TASKS.get(sid)
            live = (t is not None and not t.done()) or sid in server_shared._RUNNING_TURNS
            if live:
                server_shared._TURN_STOP.setdefault(sid, asyncio.Event()).set()

    hooks.append(ws_stop_like_handler)

    await sched._inject_result(
        "PW", sched.format_subagent_result("CW", "g", "结论"), "CW"
    )
    await _drain(fake_stream)

    meta, _ = sched._find_meta("PW")
    assert pressed  # the hook really fired inside the flip
    assert meta["subagent"]["status"] == "stopped"  # NOT done past the stop
    assert _stream_count(fake_stream, "PW") == 0  # no turn ran after the stop
    assert _stream_count(fake_stream, "ROOT") == 0
    assert "PW" not in server_shared._RUNNING_TURNS


async def test_cascade_stop_in_wake_flip_window_no_ghost_turn(
    isolated_home, fake_stream, yielding_push
):
    """Same flip window, cascade shape: stop_subagent_tree covers PZ while its
    result wake holds the lock mid-flip. Pre-fix the cascade finds NO stop
    event, direct-finalizes the subagent "stopped" OUTSIDE the lock, and the
    wake — whose under-lock checks already passed — still opens a full ghost
    turn on the stopped session."""
    _state, hooks = yielding_push
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("PZ", parent="ROOT", depth=0, status="waiting"))
    _put_meta(_sub_meta("CZ", parent="PZ", depth=0, status="done"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["PZ"] = _sess("PZ", type_="subagent")

    fired = False

    async def cascade(sid, event, data):
        nonlocal fired
        if sid == "PZ" and event == "subagent.status" and not fired:
            fired = True
            await sched.stop_subagent_tree("PZ", include_self=True)

    hooks.append(cascade)

    await sched._inject_result(
        "PZ", sched.format_subagent_result("CZ", "g", "结论"), "CZ"
    )
    await _drain(fake_stream)

    meta, _ = sched._find_meta("PZ")
    assert fired
    assert meta["subagent"]["status"] == "stopped"
    assert _stream_count(fake_stream, "PZ") == 0  # no ghost turn on the dead session
    assert _stream_count(fake_stream, "ROOT") == 0
    assert "PZ" not in server_shared._RUNNING_TURNS


# --------------------------------------------------------------------------- #
# adversarial-review round 4 (终审): double-finalize across the compression await
# --------------------------------------------------------------------------- #
async def test_concurrent_settles_finalize_once(isolated_home, capture, monkeypatch):
    """The terminal check ("meta still running?") runs BEFORE the summary work,
    and a done-finalize suspends for seconds inside the compression LLM call.
    In that window a SECOND gate invocation for the same session (e.g. a user
    invoke into the session whose wake turn just freed both busy registries —
    _TURN_TASKS and _RUNNING_TURNS are both empty) also passes the check and
    finalizes again: double compression, double broadcast, and the SAME result
    injected into the ancestor twice. The in-flight latch must collapse them to
    one finalize."""
    _put_meta(_sub_meta("P"))
    _put_meta(_sub_meta("DC", parent="P", depth=0, status="running"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_report(slug, session_id):
        return "证据与结论。" * 1000  # > SUMMARY_MAX_CHARS → compression path

    async def slow_compress(report, meta):
        entered.set()
        await release.wait()  # hold finalize #1 mid-flight
        return report[:10]

    monkeypatch.setattr(sched, "_final_report_text", fake_report)
    monkeypatch.setattr(sched, "_maybe_compress_summary", slow_compress)

    first = asyncio.create_task(sched.on_turn_settled("DC"))
    await entered.wait()  # finalize #1 is now suspended inside compression
    await sched.on_turn_settled("DC")  # the racing second gate
    release.set()
    await first
    await _ticks()  # let the spawn_bg wake run

    # count frames DELIVERED TO the parent (push_both also sends one to the
    # child's own sockets — see _status_events)
    done = [
        d
        for tgt, e, d in capture["sent"]
        if tgt == "P" and e == "subagent.status"
        and d.get("session_id") == "DC" and d.get("status") == "done"
    ]
    assert len(done) == 1  # one broadcast, not two
    assert len(capture["wakes"]) == 1  # one upward injection, not two
    meta, _ = sched._find_meta("DC")
    assert meta["subagent"]["status"] == "done"


# --------------------------------------------------------------------------- #
# recursion budget: the scheduler salvage (engine's pre-warn steer is a plain
# steer_enqueue call — its shape is covered by the stash tests above)
# --------------------------------------------------------------------------- #
async def test_recursion_error_salvages_wrap_turn_then_done(isolated_home, fake_stream):
    """A subagent turn dying on GraphRecursionError gets ONE continuation turn
    whose instruction forbids tool calls; that wrap turn settles clean and
    finalizes done with its report injected into the parent (pre-fix: failed
    with summary_len=0 and only an error riding the failure report)."""
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("CR", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["CR"] = _sess("CR", type_="subagent")
    _meta, slug = sched._find_meta("CR")
    _session_meta_patch(slug, "CR", {"last_error": {
        "turn_id": "t-cr1",
        "message": "GraphRecursionError: Recursion limit of 128 reached without hitting a stop condition.",
        "at": time.time(),
    }})
    await sched.on_turn_settled("CR", None, "t-cr1")
    await _drain(fake_stream)

    cr, _ = sched._find_meta("CR")
    assert cr["subagent"]["status"] == "done"
    assert cr["subagent"].get("recursion_wrapped") is True
    assert _stream_count(fake_stream, "CR") == 1  # exactly the wrap turn
    wrap_text = next(t for s, t in fake_stream["streams"] if s == "CR")
    assert "step limit" in wrap_text and "Do not call any more tools now" in wrap_text
    assert len(_status_events(fake_stream, "ROOT", "CR", "done")) == 1


async def test_recursion_wrap_is_one_shot(isolated_home, fake_stream):
    """A wrap-up continuation that ITSELF dies on the limit fails for real:
    the meta's recursion_wrapped flag blocks a second salvage."""
    _put_meta(_sub_meta("ROOT"))
    base = _sub_meta("CW", parent="ROOT", depth=0, status="running")
    base["subagent"]["recursion_wrapped"] = True
    _put_meta(base)
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["CW"] = _sess("CW", type_="subagent")
    _meta, slug = sched._find_meta("CW")
    _session_meta_patch(slug, "CW", {"last_error": {
        "turn_id": "t-cw2",
        "message": "GraphRecursionError: Recursion limit of 128 reached without hitting a stop condition.",
        "at": time.time(),
    }})
    await sched.on_turn_settled("CW", None, "t-cw2")
    await _drain(fake_stream)

    cw, _ = sched._find_meta("CW")
    assert cw["subagent"]["status"] == "failed"
    assert _stream_count(fake_stream, "CW") == 0  # no second salvage turn


async def test_turn_recursion_limit_sources(monkeypatch, isolated_home):
    """turn_recursion_limit: default 128, settings runtime.recursion_limit,
    GINNO_RECURSION_LIMIT wins, clamped to [25, 1000]."""
    from ginno_runtime.api.stream import engine

    monkeypatch.delenv("GINNO_RECURSION_LIMIT", raising=False)
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert engine.turn_recursion_limit() == 128

    p.write_text(json.dumps({"runtime": {"recursion_limit": 200}}))
    assert engine.turn_recursion_limit() == 200

    monkeypatch.setenv("GINNO_RECURSION_LIMIT", "300")
    assert engine.turn_recursion_limit() == 300

    monkeypatch.setenv("GINNO_RECURSION_LIMIT", "5000")
    assert engine.turn_recursion_limit() == 1000


async def test_idle_injections_coalesce_into_one_wake(monkeypatch, isolated_home, fake_stream):
    """Merged injection (open question 3, revised): two children of an idle
    MAIN parent finish back-to-back while a third is still live — the results
    must arrive as ONE wake turn carrying both, not two piecemeal summaries
    (pre-fix: each result woke the parent separately)."""
    monkeypatch.setattr(sched, "INJECTION_COALESCE_S", 0.01)
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("C1", parent="ROOT", depth=0, status="done"))
    _put_meta(_sub_meta("C2", parent="ROOT", depth=0, status="done"))
    _put_meta(_sub_meta("C3", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")

    # C3 live → first injection takes the coalesce timer, second rides it.
    await sched._inject_result("ROOT", sched.format_subagent_result("C1", "g1", "结论 A"), "C1")
    await sched._inject_result("ROOT", sched.format_subagent_result("C2", "g2", "结论 B"), "C2")
    await _drain(fake_stream, timeout_s=3.0)

    # C3 still running → only the timer flush happened: ONE merged wake.
    assert _stream_count(fake_stream, "ROOT") == 1
    wake_text = next(t for s, t in fake_stream["streams"] if s == "ROOT")
    assert "结论 A" in wake_text and "结论 B" in wake_text
    assert "returns from 2 subagents" in wake_text
    assert not sched._PENDING_INJECTIONS.get("ROOT")


async def test_flush_routes_to_steer_when_parent_went_live(monkeypatch, isolated_home, fake_stream):
    """A queued flush that finds the parent RUNNING must steer (one entry
    each) instead of opening a second turn."""
    monkeypatch.setattr(sched, "INJECTION_COALESCE_S", 0.01)
    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("C1", parent="ROOT", depth=0, status="done"))
    _put_meta(_sub_meta("C2", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")

    await sched._inject_result("ROOT", sched.format_subagent_result("C1", "g1", "结论 A"), "C1")
    # The user starts typing before the timer fires.
    server_shared._RUNNING_TURNS["ROOT"] = "turn-live"
    await _drain(fake_stream, timeout_s=3.0)

    assert _stream_count(fake_stream, "ROOT") == 0  # no wake turn opened
    stashed = server_shared._STEER_STASH.get("ROOT") or []
    assert len(stashed) == 1
    assert stashed[0]["extra_kwargs"]["ginno_subagent_result"] == "C1"
    assert stashed[0]["turn_id"] == "turn-live"


# --------------------------------------------------------------------------- #
# spawned background work must not inherit the spawning turn's LLM run context
# (2026-10-01 串线事故：父 turn 的 astream 把子代理的 token 当自己的流推了出来)
# --------------------------------------------------------------------------- #
async def test_spawn_bg_clears_inherited_llm_context(isolated_home):
    """A task spawned from inside a turn must not inherit that turn's
    ``var_child_runnable_config`` — otherwise the spawning turn's
    ``stream_mode="messages"`` surfaces the child's LLM tokens as its own."""
    from langchain_core.runnables.config import var_child_runnable_config

    seen: dict = {}

    async def background():
        seen["cfg"] = var_child_runnable_config.get()

    token = var_child_runnable_config.set({"callbacks": ["parent-run"]})
    try:
        task = spawn_bg(background())
        await task
    finally:
        var_child_runnable_config.reset(token)

    assert seen["cfg"] is None, "background task inherited the parent's run config"


async def test_child_turn_does_not_inherit_parent_llm_context(
    isolated_home, fake_stream, monkeypatch
):
    """The scheduler path end to end: with a parent run context active, the
    child's managed turn starts from a clean context."""
    from langchain_core.runnables.config import var_child_runnable_config

    _put_meta(_sub_meta("ROOT"))
    _put_meta(_sub_meta("CX", parent="ROOT", depth=0, status="running"))
    server_shared._SESSIONS["ROOT"] = _sess("ROOT")
    server_shared._SESSIONS["CX"] = _sess("CX", type_="subagent")

    seen: dict = {}

    async def probing_run_stream(*a, **kw):
        seen["cfg"] = var_child_runnable_config.get()

    monkeypatch.setattr(stream_mod, "_run_stream", probing_run_stream)

    token = var_child_runnable_config.set({"callbacks": ["parent-run"]})
    try:
        await sched._run_managed_turn("CX", "子任务", None, asyncio.Event())
    finally:
        var_child_runnable_config.reset(token)

    assert seen.get("cfg") is None, "child turn inherited the parent's run config"
