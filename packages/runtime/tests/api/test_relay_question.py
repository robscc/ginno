"""Worker ask_user 中断透传（relay-question 设计）单测。

覆盖 goal 七件中的验收面：

1. **qid 凭证链三层**：ask_tools 生成（schema enum 提示层）/ 服务端
   parked 校验（choice 硬校验 + worker_session 匹配）/ 即销墓碑。
2. **幂等零影响**：qid 已答后 relay_answer 命中墓碑，纯只读四不
   （不 resume / 不写 checkpoint / 不广播 / 不动注册表）。
3. **条件注入**：登记表非空才产出 relay_answer 工具，qid enum 只含待答。
4. **多问题列表化**：pending_questions 列表字段 + choice 数组映射。
5. **无订阅者零回归**：无 subscribers 的 parked 不登记、不注入。
6. **resume_question 共享段**：payload 组装 / arm / 墓碑收口。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ginno_runtime import server_shared
from ginno_runtime.api.stream import resume as resume_mod
from ginno_runtime.tools import ask_tools as at
from ginno_runtime.worker_bus import question_sync, relay


@pytest.fixture(autouse=True)
def _clean_relay_state():
    """relay 模块级登记表/墓碑每测试前清空（isolated_home 换 GINNO_HOME，
    但内存表是进程级——e2e/其他段的残留会污染这里的断言）。"""
    relay._reset_state()
    yield
    relay._reset_state()
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _tool():
    return at.build_ask_tools("default", "sess-worker")[0]


def _capture_interrupt(monkeypatch, ret=None):
    seen = {}

    def _fake(value):
        seen.update(value)
        return ret if ret is not None else {
            "kind": "user_answer", "answer": "", "option_index": None,
            "skip": True,
        }

    monkeypatch.setattr(at, "interrupt", _fake)
    return seen


def _fake_meta(monkeypatch, subscribers):
    """让 relay._find_meta 返回带 subscribers 的 worker meta。"""

    def _find(sid):
        if sid == "sess-worker":
            return (
                {
                    "id": "sess-worker",
                    "title": "迁移工人",
                    "type": "subagent",
                    "subscribers": [
                        {"session_id": s, "want": "digest"} for s in subscribers
                    ],
                },
                "default",
            )
        return None

    monkeypatch.setattr(relay, "_find_meta", _find)


def _capture_pushes(monkeypatch):
    """记录 _push_session_event / _inject_result 调用（relay 的两个出口）。"""
    events, injects = [], []

    async def _push(sid, kind, payload, turn_id=""):
        events.append((sid, kind, payload))

    async def _inject(sid, text, child, extra_kwargs=None):
        injects.append((sid, text, child, extra_kwargs))

    monkeypatch.setattr(relay, "_push_session_event", _push)
    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_inject_result", _inject)
    return events, injects


def _capture_resume(monkeypatch, ok=True):
    """捕获 resume_pending_interrupt（不发真 task）。"""
    calls = []

    def _fake(session_id, resume_value, *, base_config=None):
        calls.append((session_id, resume_value, base_config))
        return ok

    monkeypatch.setattr(resume_mod, "resume_pending_interrupt", _fake)
    return calls


async def _park(monkeypatch, subscribers=("sess-sub",), **value_extra):
    """最小 parked 场景：ask_user 生成 qid → note_parked_question 登记。"""
    relay._reset_state()
    _fake_meta(monkeypatch, list(subscribers))
    _capture_pushes(monkeypatch)
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user", "args": {"question": "装到哪？",
                                      "options": ["Ginno 全局", "装到仓库"],
                                      "header": "选择安装位置"},
         "id": "tc-1", "type": "tool_call"}
    )
    value = dict(seen)
    value.update(value_extra)
    qid = await relay.note_parked_question("sess-worker", "turn-1", value)
    return qid, value


async def _park_value(monkeypatch, subscribers):
    """_park 的变体：只走 ask_user 生成 value + 登记，返回 (qid, value)。"""
    relay._reset_state()
    _fake_meta(monkeypatch, list(subscribers))
    _capture_pushes(monkeypatch)
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user", "args": {"question": "装到哪？",
                                      "options": ["Ginno 全局", "装到仓库"],
                                      "header": "选择安装位置"},
         "id": "tc-1", "type": "tool_call"}
    )
    value = dict(seen)
    qid = await relay.note_parked_question("sess-worker", "turn-1", value)
    return qid, value


# --------------------------------------------------------------------------- #
# 1) qid 凭证链：生成 → 登记 → 即销
# --------------------------------------------------------------------------- #


async def test_qid_generated_in_interrupt_payload(isolated_home, monkeypatch):
    """第一层：qid 由 ask_user 随 interrupt payload 生成（checkpoint 载体）。"""
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user", "args": {"question": "q"}, "id": "tc-9",
         "type": "tool_call"}
    )
    qid = seen.get("qid")
    assert isinstance(qid, str) and qid.startswith("q_") and len(qid) == 14


async def test_parked_registers_subscribers_and_payload(isolated_home, monkeypatch):
    """第二层：有订阅者才登记；payload/订阅者/注入齐备。"""
    qid, _ = await _park(monkeypatch, subscribers=("sess-sub", "sess-sub2"))
    assert qid and qid in relay._PENDING_RELAY
    ent = relay._PENDING_RELAY[qid]
    assert ent["worker_sid"] == "sess-worker"
    assert ent["worker_title"] == "迁移工人"
    assert ent["subscribers"] == ["sess-sub", "sess-sub2"]
    assert ent["payload"]["options"] == ["Ginno 全局", "装到仓库"]
    # ③ 注入模板：两个订阅者各一条，含编排指令关键语
    # （_capture_pushes 在 _park 内部挂的 patch 仍生效——直接看模块调用记
    # 录不可行，改为查 entry.asked 已记账 + 重新验证模板文本函数）。
    assert set(ent.get("asked") or {}) == {"sess-sub", "sess-sub2"}
    text = relay._relay_instructions(ent)
    assert "relay_answer" in text and "ask_user" in text
    assert "完整文本" in text or "original option text" in text
    assert "qid" in text and "sess-worker" in text


async def test_pending_questions_payload_fields(isolated_home, monkeypatch):
    """② digest 列表字段契约：qid/worker_sid/turn_id/header/options/multi/
    prefill/worker_title。"""
    qid, _ = await _park(monkeypatch)
    rows = relay._pending_questions_payload("sess-sub")
    assert len(rows) == 1
    row = rows[0]
    assert row["qid"] == qid
    assert row["worker_sid"] == "sess-worker"
    assert row["turn_id"] == "turn-1"
    assert row["header"] == "选择安装位置"
    assert row["options"] == ["Ginno 全局", "装到仓库"]
    assert row["multi"] is False
    assert "prefill" in row and row["worker_title"] == "迁移工人"


async def test_no_subscribers_zero_regression(isolated_home, monkeypatch):
    """⑤ 无订阅者：不登记、不注入——既有 parked 行为零变化。"""
    events, injects = _capture_pushes(monkeypatch)
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user", "args": {"question": "q", "options": ["a", "b"]},
         "id": "tc-1", "type": "tool_call"}
    )
    qid = await relay.note_parked_question("sess-worker", "turn-1", dict(seen))
    assert qid is None
    assert relay._PENDING_RELAY == {}
    assert events == [] and injects == []


# --------------------------------------------------------------------------- #
# 2) relay_answer：命中 / choice 硬校验 / 幂等零影响
# --------------------------------------------------------------------------- #


async def test_relay_answer_success_maps_choice(isolated_home, monkeypatch):
    """命中：choice 硬校验 + 映射回原选项（label + index）+ 墓碑即销。"""
    qid, _ = await _park(monkeypatch)
    calls = _capture_resume(monkeypatch, ok=True)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", qid, "装到仓库", "用户说放 repo"
        )
    )
    assert out["ok"] is True and out["resumed"] is True
    assert out["relayed_by"] == "sess-sub"
    sid, value, _cfg = calls[0]
    assert sid == "sess-worker"
    assert value["kind"] == "user_answer"
    assert value["answer"] == "装到仓库" and value["option_index"] == 1
    # 即销：pending 空、墓碑带溯源
    assert relay._PENDING_RELAY == {}
    tb = relay.tombstone_of(qid)
    assert tb["answered_by"] == "sess-sub"
    assert tb["answer"] == "装到仓库"
    assert tb.get("relayed_by") == "sess-sub"
    assert tb["answered_at"] > 0


async def test_relay_answer_choice_hard_validation(isolated_home, monkeypatch):
    """choice 不在原选项集 → 报错列合法选项（LLM 自纠），不 resume。"""
    qid, _ = await _park(monkeypatch)
    calls = _capture_resume(monkeypatch)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", qid, "随便装哪", "r"
        )
    )
    assert out["ok"] is False
    assert "装到仓库" in out["error"] and "Ginno 全局" in out["error"]
    assert calls == []
    assert qid in relay._PENDING_RELAY  # 未消耗


async def test_relay_answer_requires_mapping_reason(isolated_home, monkeypatch):
    qid, _ = await _park(monkeypatch)
    calls = _capture_resume(monkeypatch)
    out = json.loads(
        await relay.relay_answer_impl("sess-sub", "sess-worker", qid, "装到仓库", " ")
    )
    assert out["ok"] is False and "mapping_reason" in out["error"]
    assert calls == []


async def test_relay_answer_worker_session_mismatch(isolated_home, monkeypatch):
    qid, _ = await _park(monkeypatch)
    calls = _capture_resume(monkeypatch)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-other", qid, "装到仓库", "r"
        )
    )
    assert out["ok"] is False and "sess-worker" in out["error"]
    assert calls == []


async def test_relay_answer_already_answered_readonly(isolated_home, monkeypatch):
    """幂等零影响（四不）：qid 已答 → 纯只读返回墓碑 shape。"""
    qid, _ = await _park(monkeypatch)
    relay.mark_answered(qid, "user", "Ginno 全局")
    # 四不监控：resume / 注册表 / meta 写 / 广播
    calls = _capture_resume(monkeypatch)
    events, injects = _capture_pushes(monkeypatch)
    import ginno_runtime.session_meta as sm

    patches = []
    monkeypatch.setattr(
        sm, "_session_meta_patch",
        lambda *a, **k: patches.append(a),
    )
    snap = (
        set(server_shared._PENDING_RESUME),
        dict(server_shared._PENDING_KIND),
        dict(server_shared._TURN_TASKS),
        dict(relay._RELAY_TOMBSTONES),
    )
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", qid, "装到仓库", "r"
        )
    )
    assert out == {
        "ok": False,
        "already_answered": True,
        "answered_by": "user",
        "answer": "Ginno 全局",
        "answered_at": snap[3][qid]["answered_at"],
        "note": out["note"],
    }
    assert out["note"]  # 说明文案非空
    # 四不断言：零调用、零状态变化
    assert calls == [] and events == [] and injects == [] and patches == []
    assert snap == (
        set(server_shared._PENDING_RESUME),
        dict(server_shared._PENDING_KIND),
        dict(server_shared._TURN_TASKS),
        dict(relay._RELAY_TOMBSTONES),
    )


async def test_relay_answer_unknown_qid(isolated_home, monkeypatch):
    relay._reset_state()
    calls = _capture_resume(monkeypatch)
    events, injects = _capture_pushes(monkeypatch)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", "q_nope", "x", "r"
        )
    )
    assert out["ok"] is False and "unknown" in out["error"].lower()
    assert calls == [] and events == [] and injects == []


# --------------------------------------------------------------------------- #
# 3) 多问题列表化
# --------------------------------------------------------------------------- #


async def test_multi_question_card_list_and_array_choice(isolated_home, monkeypatch):
    """多问题卡：payload 列表化 + choice 数组逐题映射。"""
    relay._reset_state()
    _fake_meta(monkeypatch, ["sess-sub"])
    _capture_pushes(monkeypatch)
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user",
         "args": {"question": "两个决定",
                  "questions": [
                      {"question": "装到哪？", "options": ["全局", "仓库"],
                       "header": "位置"},
                      {"question": "何时？", "options": ["现在", "稍后"],
                       "header": "时间"},
                  ]},
         "id": "tc-1", "type": "tool_call"}
    )
    assert seen.get("multi") is True and len(seen["questions"]) == 2
    qid = await relay.note_parked_question("sess-worker", "turn-1", dict(seen))
    rows = relay._pending_questions_payload("sess-sub")
    assert rows[0]["multi"] is True
    calls = _capture_resume(monkeypatch, ok=True)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", qid, ["仓库", "现在"], "逐题映射"
        )
    )
    assert out["ok"] is True
    value = calls[0][1]
    assert value["multi"] is True
    assert [a["answer"] for a in value["answers"]] == ["仓库", "现在"]
    assert [a["option_index"] for a in value["answers"]] == [1, 0]


async def test_multi_question_wrong_length_choice(isolated_home, monkeypatch):
    relay._reset_state()
    _fake_meta(monkeypatch, ["sess-sub"])
    _capture_pushes(monkeypatch)
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user",
         "args": {"questions": [
             {"question": "a", "options": ["1", "2"]},
             {"question": "b", "options": ["3", "4"]},
         ]},
         "id": "tc-1", "type": "tool_call"}
    )
    qid = await relay.note_parked_question("sess-worker", "t", dict(seen))
    calls = _capture_resume(monkeypatch)
    out = json.loads(
        await relay.relay_answer_impl(
            "sess-sub", "sess-worker", qid, ["1"], "r"
        )
    )
    assert out["ok"] is False and calls == []


async def test_multiple_pending_questions_in_list(isolated_home, monkeypatch):
    """多 worker / 同 worker 多问：列表聚合 + qid enum 全含。"""
    q1, _ = await _park(monkeypatch)
    _fake_meta(monkeypatch, ["sess-sub"])  # 第二问同 worker
    seen = _capture_interrupt(monkeypatch)
    _tool().invoke(
        {"name": "ask_user", "args": {"question": "第二问",
                                      "options": ["x", "y"]},
         "id": "tc-2", "type": "tool_call"}
    )
    q2 = await relay.note_parked_question("sess-worker", "turn-2", dict(seen))
    assert q1 != q2
    rows = relay._pending_questions_payload("sess-sub")
    assert [r["qid"] for r in rows] == [q1, q2]
    assert [r["turn_id"] for r in rows] == ["turn-1", "turn-2"]
    tool = relay.build_relay_tool("sess-sub")
    assert tool is not None
    # enum 提示在 description 里逐 qid 列出
    assert all(q in tool.args["qid"]["description"] for q in (q1, q2))


# --------------------------------------------------------------------------- #
# 4) 条件注入开关（⑥）
# --------------------------------------------------------------------------- #


async def test_build_relay_tool_conditional(isolated_home, monkeypatch):
    """登记表非空才注入；答完即消失。"""
    relay._reset_state()
    assert relay.build_relay_tool("sess-sub") is None
    assert relay.build_relay_tool(None) is None
    qid, _ = await _park(monkeypatch)
    tool = relay.build_relay_tool("sess-sub")
    assert tool is not None and tool.name == "relay_answer"
    assert qid in tool.args["qid"]["description"]
    # 非订阅者：无登记 → 不注入
    assert relay.build_relay_tool("sess-other") is None
    # 已答：即销 → 工具消失
    relay.mark_answered(qid, "user", "Ginno 全局")
    assert relay.build_relay_tool("sess-sub") is None


async def test_build_relay_tool_asked_reminder(isolated_home, monkeypatch):
    """已问未答：工具描述标注提醒。"""
    qid, _ = await _park(monkeypatch)
    tool = relay.build_relay_tool("sess-sub")
    assert tool is not None
    assert "already relayed" in tool.description or "已问过" in tool.description


async def test_graph_relay_tool_helper(isolated_home, monkeypatch):
    """graph.py 的 bind 点 helper：登记后才产出（agent_node 补绑的入口）。"""
    from ginno_runtime import graph as g

    assert g._relay_tool_for_session("sess-sub") is None
    assert g._relay_tool_for_session("") is None
    qid, _ = await _park(monkeypatch)
    tool = g._relay_tool_for_session("sess-sub")
    assert tool is not None and tool.name == "relay_answer"
    relay.mark_answered(qid, "user", "Ginno 全局")
    assert g._relay_tool_for_session("sess-sub") is None


async def test_relay_tool_invocable(isolated_home, monkeypatch):
    """产出的工具可被 tool call 协议执行（args_schema 校验 + async body）。"""
    qid, _ = await _park(monkeypatch)
    calls = _capture_resume(monkeypatch, ok=True)
    tool = relay.build_relay_tool("sess-sub")
    out = await tool.ainvoke(
        {"name": "relay_answer",
         "args": {"worker_session": "sess-worker", "qid": qid,
                  "choice": "装到仓库", "mapping_reason": "用户选了 repo"},
         "id": "tc-r", "type": "tool_call"}
    )
    text = out.content if hasattr(out, "content") else out
    assert json.loads(text)["ok"] is True
    assert calls[0][1]["answer"] == "装到仓库"


# --------------------------------------------------------------------------- #
# 5) resume_question 共享段（⑤）
# --------------------------------------------------------------------------- #


async def test_resume_question_single(isolated_home, monkeypatch):
    server_shared._PENDING_RESUME.add("sess-worker")
    server_shared._PENDING_KIND["sess-worker"] = "user_question"
    calls = _capture_resume(monkeypatch, ok=True)
    ok = await resume_mod.resume_question(
        "sess-worker", answer="装到仓库", option_index=1, base_config={"c": 1}
    )
    assert ok is True
    sid, value, cfg = calls[0]
    assert sid == "sess-worker" and cfg == {"c": 1}
    assert value == {"kind": "user_answer", "answer": "装到仓库",
                     "option_index": 1, "skip": False}
    # 守卫由 resume_pending_interrupt 的 check-and-pop 清（此处被 fake 了，
    # 手动清防泄漏到后续测试）
    server_shared._PENDING_RESUME.discard("sess-worker")
    server_shared._PENDING_KIND.pop("sess-worker", None)


async def test_resume_question_multi_fallback(isolated_home, monkeypatch):
    """multi 且 answers 空 → 兜底 skip 条目（与原 ws 分支逐字节同语义）。"""
    server_shared._PENDING_RESUME.add("s")
    server_shared._PENDING_KIND["s"] = "user_question"
    calls = _capture_resume(monkeypatch)
    await resume_mod.resume_question("s", multi=True, answers=[])
    value = calls[0][1]
    assert value["multi"] is True
    assert value["answers"] == [{"answer": "", "option_index": None, "skip": True}]
    server_shared._PENDING_RESUME.discard("s")
    server_shared._PENDING_KIND.pop("s", None)


async def test_resume_question_tombstones_relay(isolated_home, monkeypatch):
    """共享段统一墓碑收口：用户直答后 relay 登记即销。"""
    qid, _ = await _park(monkeypatch)
    server_shared._PENDING_RESUME.add("sess-worker")
    server_shared._PENDING_KIND["sess-worker"] = "user_question"
    _capture_resume(monkeypatch, ok=True)
    await resume_mod.resume_question(
        "sess-worker", answer="Ginno 全局", option_index=0, answered_by="user"
    )
    assert relay._PENDING_RELAY == {}
    tb = relay.tombstone_of(qid)
    assert tb["answered_by"] == "user" and tb["answer"] == "Ginno 全局"
    server_shared._PENDING_RESUME.discard("sess-worker")
    server_shared._PENDING_KIND.pop("sess-worker", None)


def test_sync_multi_card_matches_options_by_label():
    original = {"multi": True, "questions": [
        {"question": "Where?", "options": ["local", "shared"]},
        {"question": "When?", "options": ["today", "later"]},
    ]}
    selections = question_sync.selected_options(original, {
        "multi": True, "answers": [{"option_index": 1}, {"option_index": 0}],
    })
    forwarded = {"multi": True, "questions": [
        {"question": "Date?", "options": ["later", "today"]},
        {"question": "Destination?", "options": ["shared", "local"]},
    ]}
    assert question_sync._map_selections(selections, forwarded) == {
        "multi": True, "answers": [
            {"answer": "today", "option_index": 1}, {"answer": "shared", "option_index": 0},
        ],
    }
    forwarded["questions"][0]["options"] = ["today", "tomorrow"]
    assert question_sync._map_selections(selections, forwarded) is None
    assert question_sync.selected_options(original, {
        "multi": True, "answers": [{"option_index": 1}, {"skip": True}],
    }) == []


def test_sync_requires_trusted_relay_origin():
    from langchain_core.messages import AIMessage, HumanMessage

    envelope = '<ginno_worker_question worker="worker" qid="q_worker">request</ginno_worker_question>'
    marker = {"ginno_worker_msg": {"kind": "question", "from": "worker", "qid": "q_worker"}}
    origin = HumanMessage(content=envelope, additional_kwargs=marker, id="before-ask")
    card = AIMessage(content="", tool_calls=[{"name": "ask_user", "args": {}, "id": "card"}])
    earlier = AIMessage(content="", tool_calls=[{"name": "ask_user", "args": {}, "id": "earlier"}])

    assert question_sync._relay_origin([origin, card], "card") == (
        "worker", "q_worker", envelope, "before-ask",
    )
    assert question_sync._relay_origin([HumanMessage(content=envelope), card], "card") is None
    assert question_sync._relay_origin([origin, earlier, card], "card") is None
    assert question_sync._relay_origin([origin, HumanMessage(content="Another question"), card], "card") is None


async def test_resume_question_arm_from_checkpoint(isolated_home, monkeypatch):
    """arm 分支：守卫丢失时以 checkpointer 事实武装（steer 即答语义）。"""

    class _Intr:
        value = {"kind": "user_question", "qid": "q_x"}

    class _Ckpt:
        def get_pending_interrupt(self, cfg):
            return [_Intr()]

    class _Graph:
        checkpointer = _Ckpt()

    session = {"graph": _Graph()}
    calls = _capture_resume(monkeypatch)
    ok = await resume_mod.resume_question(
        "sess-worker", answer="a", arm_from_checkpoint=True, session=session
    )
    assert ok is True and "sess-worker" in server_shared._PENDING_RESUME
    server_shared._PENDING_RESUME.discard("sess-worker")
    server_shared._PENDING_KIND.pop("sess-worker", None)


async def test_resume_question_rejected_no_tombstone(isolated_home, monkeypatch):
    """拒绝路径不写墓碑（问题仍可答）。"""
    qid, _ = await _park(monkeypatch)
    _capture_resume(monkeypatch, ok=False)
    ok = await resume_mod.resume_question("sess-worker", answer="x")
    assert ok is False
    assert qid in relay._PENDING_RELAY and relay.tombstone_of(qid) is None


# --------------------------------------------------------------------------- #
# 7) engine 层登记点（permission.handle_interrupt）集成
# --------------------------------------------------------------------------- #


async def test_handle_interrupt_defers_registration_and_emit(isolated_home, monkeypatch):
    """The engine records the interrupt in-stream and publishes it after drain."""
    from ginno_runtime.api.stream.stages import permission as perm
    from ginno_runtime.api.stream.stages.ctx import StreamTurnCtx

    qid, value = await _park_value(monkeypatch, ["sess-sub"])
    ctx = StreamTurnCtx(None, {"configurable": {"thread_id": "sess-worker"}}, None, None)
    ctx.turn_id = "turn-h"
    sent = []

    async def _send(frame):
        sent.append(frame)

    ctx.emit = lambda kind, payload: {"kind": kind, "payload": payload}
    ctx.safe_send = _send
    await perm.handle_interrupt(ctx, type("I", (), {"value": value})())
    assert ctx.saw_interrupt
    assert not sent and "sess-worker" not in server_shared._PENDING_KIND
    assert ctx.parked_questions == [value]

    await perm.publish_parked_question(ctx, ctx.parked_questions[0])
    assert server_shared._PENDING_KIND.get("sess-worker") == "user_question"
    assert relay.find_pending(qid)["turn_id"] == "turn-h"
    assert len(sent) == 1
    frame = sent[0]
    assert frame["kind"] == "user.question"
    assert frame["payload"]["qid"] == qid
    assert frame["payload"]["options"] == ["Ginno 全局", "装到仓库"]




async def test_handle_interrupt_no_subscribers_noop(isolated_home, monkeypatch):
    """无订阅者：engine 钩子零副作用（登记表空、注入零调用）。"""
    from ginno_runtime.api.stream.stages import permission as perm
    from ginno_runtime.api.stream.stages.ctx import StreamTurnCtx

    relay._reset_state()
    events, injects = _capture_pushes(monkeypatch)

    def _find_none(sid):
        return None

    monkeypatch.setattr(relay, "_find_meta", _find_none)
    ctx = StreamTurnCtx(None, {"configurable": {"thread_id": "sess-lonely"}}, None, None)
    ctx.turn_id = "t"
    sent = []

    async def _send(frame):
        sent.append(frame)

    ctx.emit = lambda kind, payload: {"kind": kind, "payload": payload}
    ctx.safe_send = _send
    await perm.handle_interrupt(
        ctx,
        type("I", (), {"value": {"kind": "user_question", "qid": "q_l",
                                 "question": "q", "options": ["a", "b"]}})(),
    )
    assert not sent
    await perm.publish_parked_question(ctx, ctx.parked_questions[0])
    assert relay._PENDING_RELAY == {}
    assert events == [] and injects == []
    assert len(sent) == 1 and sent[0]["payload"]["qid"] == "q_l"  # 事件照发（前端契约不变）


# --------------------------------------------------------------------------- #
# 6) 落盘 / 恢复
# --------------------------------------------------------------------------- #


async def test_state_persist_and_load(isolated_home, monkeypatch):
    qid, _ = await _park(monkeypatch)
    assert relay._state_path().exists()
    data = json.loads(relay._state_path().read_text())
    assert qid in data["pending"]
    relay.mark_answered(qid, "user", "Ginno 全局")
    assert qid in json.loads(relay._state_path().read_text())["tombstones"]
    # 重启恢复
    relay._reset_state()
    relay.load_state()
    assert relay.tombstone_of(qid) is not None


async def test_tombstone_ttl(monkeypatch, isolated_home):
    relay._reset_state()
    import time as _t

    relay._RELAY_TOMBSTONES["q_old"] = {
        "answered_by": "u", "answer": "a",
        "answered_at": int(_t.time()) - relay._TOMBSTONE_TTL_S - 1,
    }
    relay._persist_unlocked()
    assert "q_old" not in relay._RELAY_TOMBSTONES


async def _register(monkeypatch, question: str):
    """在不清空既有登记表的前提下追加注册一条 pending（persist 走真实
    原子写），返回 qid——与 ``_park`` 不同，不 reset（reset 会把先前
    注册条目从内存抹掉，破坏「多轮落盘累积」的测试前提）。"""
    _fake_meta(monkeypatch, ["sess-sub"])
    _capture_pushes(monkeypatch)
    value = {"kind": "user_question", "qid": "", "question": question,
             "options": ["Ginno 全局", "装到仓库"], "header": "选择安装位置"}
    return await relay.note_parked_question("sess-worker", "turn-1", value)


def _tmp_leftovers() -> list:
    """GINNO_HOME 下的 relay 临时文件（成功写完 / 失败清理后都应为空）。"""
    home = relay._state_path().parent
    return sorted(home.glob(f".{relay._STATE_FILENAME}.tmp*"))


async def test_persist_normal_update_overwrites(isolated_home, monkeypatch):
    """正常更新也覆盖：原子写成功路径下，新内容整体替换旧文件（覆盖而非
    追加/双写），且不留 tmp 残留。"""
    relay._reset_state()
    q1 = await _register(monkeypatch, "q-one")
    q2 = await _register(monkeypatch, "q-two")
    data = json.loads(relay._state_path().read_text())
    assert set(data["pending"]) == {q1, q2}          # 两次登记都落盘
    assert data["tombstones"] == {}
    relay.mark_answered(q1, "user", "Ginno 全局")     # pending → 墓碑
    data = json.loads(relay._state_path().read_text())
    assert set(data["pending"]) == {q2}              # q1 已从盘上 pending 移除
    assert set(data["tombstones"]) == {q1}           # 覆盖写带出了新墓碑
    assert _tmp_leftovers() == []
    # 清空内存重载：盘上状态完整回得来（覆盖写没有吃掉任何一条）。
    relay._reset_state()
    relay.load_state()
    assert relay.find_pending(q2) is not None
    assert relay.tombstone_of(q1) is not None


async def test_persist_replace_failure_keeps_old_state(
    isolated_home, monkeypatch, caplog
):
    """崩溃半写防护：注入 replace 前失败 → 真文件保持上一次完整旧内容，
    tmp 被清理、失败有日志；清空内存重新加载后旧 pending / tombstone
    均保留，本次（未落盘的）新登记按设计丢弃。"""
    import logging
    import os as _os

    relay._reset_state()
    q1 = await _register(monkeypatch, "q-one")
    relay.mark_answered(q1, "user", "Ginno 全局")
    q2 = await _register(monkeypatch, "q-two")
    p = relay._state_path()
    good = p.read_text()                              # 上次成功写的完整内容

    real_replace = _os.replace

    def _boom(src, dst):
        # 只对 relay-state.json 的 replace 注入失败（replace 前/即失败 =
        # 崩溃半写窗口），不干扰进程内其它写。
        if str(dst) == str(p):
            raise OSError("simulated crash before replace")
        return real_replace(src, dst)

    monkeypatch.setattr(relay.os, "replace", _boom)
    with caplog.at_level(logging.ERROR, logger="ginno.turn"):
        q3 = await _register(monkeypatch, "q-three")  # persist 失败（静默）
    assert q3 is not None

    # 真文件未被破坏：字节级等于旧完整内容（覆盖写没有发生）。
    assert p.read_text() == good
    # 临时文件已清理，不留垃圾。
    assert _tmp_leftovers() == []
    # 失败有日志。
    assert any("relay_state_persist_failed" in r.getMessage()
               for r in caplog.records)

    # 清空内存重新加载：旧 pending + 旧 tombstone 均保留。
    relay._reset_state()
    relay.load_state()
    assert relay.find_pending(q2) is not None         # 旧 pending 保留
    assert relay.tombstone_of(q1) is not None         # 旧墓碑保留
    assert relay.find_pending(q3) is None             # 失败批次按设计丢弃
