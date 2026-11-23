"""messages_ui 回放分泡边界回归：turn 触发器不焊进旧气泡。

背景（用户反馈 2026-11-18，会话 1a0c1d77 实锤）：`[task finished]` 回执
（ginno_task_event）与子代理结果（ginno_subagent_result）都是**新 turn 的
触发器**，但回放分组把它们 append 进当前 assistant 累积器且不 flush——
新 turn 的内容全部焊进上一 turn 的气泡（实例：111 块巨无霸，两个已完成
turn 的产出合体，第二 turn 连用户气泡都没有）。live 视图本就是新气泡起
步，回放必须同构：触发器先 flush，再挂 pending_bands 到新气泡头部。
"""

from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime.api.messages_ui import _messages_to_ui


def _bubbles(ui):
    return [m for m in ui if m.get("role") == "assistant"]


def test_task_event_receipt_starts_new_bubble():
    """[task finished] 回执后的 ai 内容必须是**新气泡**，回执挂其头部。"""
    h1 = HumanMessage(content="派活：修 X", id="u1")
    a1 = AIMessage(content="第一轮完成 ✅", id="a1")
    te = HumanMessage(
        content='[task finished] "watch" (t_1) exit 0',
        id="te1",
        additional_kwargs={"ginno_task_event": {
            "task": "t_1", "kind": "exit", "title": "watch", "exit": 0, "dur": "1m",
        }},
    )
    a2 = AIMessage(content="收尾汇报 ✅", id="a2")
    ui = _messages_to_ui([h1, a1, te, a2], None)

    bubbles = _bubbles(ui)
    assert len(bubbles) == 2, f"应拆成 2 个 assistant 气泡，实际 {len(bubbles)}"
    texts1 = [b.get("text") for b in bubbles[0]["blocks"] if b.get("kind") == "text"]
    texts2 = [b.get("text") for b in bubbles[1]["blocks"] if b.get("kind") == "text"]
    assert any("第一轮" in t for t in texts1)
    assert any("收尾汇报" in t for t in texts2)
    # 回执块挂在新气泡头部（与 live 视图一致），旧气泡不含回执。
    assert bubbles[1]["blocks"][0].get("kind") == "task_event"
    assert all(b.get("kind") != "task_event" for b in bubbles[0]["blocks"])


def test_subagent_result_starts_new_bubble():
    """子代理结果回传后的 ai 内容必须是新气泡（同 task_event 契约）。"""
    h1 = HumanMessage(content="开工", id="u1")
    a1 = AIMessage(content="已派出子代理", id="a1")
    sr = HumanMessage(
        content="<ginno_subagent_result session='s1'>报告全文</ginno_subagent_result>",
        id="sr1",
        additional_kwargs={"ginno_subagent_result": {"session": "s1"}},
    )
    a2 = AIMessage(content="汇总子代理产出 ✅", id="a2")
    ui = _messages_to_ui([h1, a1, sr, a2], None)

    bubbles = _bubbles(ui)
    assert len(bubbles) == 2
    texts2 = [b.get("text") for b in bubbles[1]["blocks"] if b.get("kind") == "text"]
    assert any("汇总" in t for t in texts2)


def test_steer_band_still_merges_into_same_bubble():
    """回归护栏：真正的 mid-turn steer 仍并入当前气泡（§3.5 契约不变）。"""
    h1 = HumanMessage(content="开始", id="u1")
    a1 = AIMessage(content="步骤一", id="a1")
    st = HumanMessage(
        content="补充要求：改用方案 B",
        id="st1",
        additional_kwargs={"ginno_steer": {"steer_id": "s1", "text": "补充要求：改用方案 B"}},
    )
    a2 = AIMessage(content="吸收 steer 后继续 ✅", id="a2")
    ui = _messages_to_ui([h1, a1, st, a2], None)

    bubbles = _bubbles(ui)
    assert len(bubbles) == 1, "steer 是 mid-turn 注入，不得拆泡"
    kinds = [b.get("kind") for b in bubbles[0]["blocks"]]
    assert "steer" in kinds


def test_receipt_before_any_ai_renders_standalone_then_attaches():
    """会话以回执开场的边界：无旧泡可 flush，回执随新气泡渲染不丢。"""
    te = HumanMessage(
        content="[task finished] t_9 exit 0",
        id="te0",
        additional_kwargs={"ginno_task_event": {
            "task": "t_9", "kind": "exit", "title": "bg", "exit": 0, "dur": "2m",
        }},
    )
    a1 = AIMessage(content="被回执唤起的首个回复", id="a9")
    ui = _messages_to_ui([te, a1], None)

    bubbles = _bubbles(ui)
    assert len(bubbles) == 1
    assert bubbles[0]["blocks"][0].get("kind") == "task_event"


def test_coalesced_plan_and_subagent_returns_keep_order():
    combined = HumanMessage(
        id="batched-returns",
        content=('Below are the returns from 2 subagents:\n\n'
                 '<ginno_plan_update plan="pl_demo">Build done</ginno_plan_update>\n\n'
                 '<ginno_subagent_result session="s_demo">Result done</ginno_subagent_result>'),
        additional_kwargs={"ginno_subagent_result": "plan-pl_demo,s_demo"},
    )
    bubbles = _bubbles(_messages_to_ui([combined, AIMessage(content="ack")], None))
    assert bubbles[0]["blocks"] == [
        {"kind": "plan_update", "planId": "pl_demo", "text": "Build done"},
        {"kind": "subagent_result", "sessionId": "s_demo", "goal": "", "summary": "Result done"},
        {"kind": "text", "text": "ack"},
    ]
