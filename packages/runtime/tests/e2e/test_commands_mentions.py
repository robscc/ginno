"""WebSocket E2E: slash commands + @mentions through the real graph.

Covers: /skill substitution reaches the model as a HumanMessage; filesystem
paths (`/tmp/...`) pass through untouched; /help short-circuits with a notice
event and NO model call; @artifact rides attached_files without duplicating
panel rows; @workflow / @memory inject <mentioned_*> system sections; @agent
overrides routing; raw @kind:label tokens resolve without a structured list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import event_names, events_of
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from ginno_runtime import artifacts as art_store
from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime import workflows as wf_store
from ginno_runtime.files import reset_registries
from ginno_runtime.world_state import TURN_CONTEXT_PREFIX

pytestmark = pytest.mark.e2e


def turn_context_of(model) -> str:
    """The last per-turn context message (plan B1) the model received."""
    for h in reversed(model._humans):
        if isinstance(h, str) and h.startswith(TURN_CONTEXT_PREFIX):
            return h
    return ""


@pytest.fixture(autouse=True)
def _fresh_files(isolated_home):
    # The file registry keeps module-level globals; isolated_home does not
    # reset them (same guard as test_files_ws.py).
    reset_registries()
    yield
    reset_registries()


class CapturingModel(BaseChatModel):
    """Fixed reply; records every system prompt and the last human message."""

    reply: str = "ok"
    _captured: list = PrivateAttr(default_factory=list)  # system prompts
    _humans: list = PrivateAttr(default_factory=list)  # human message contents

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _capture(self, messages) -> None:
        for m in messages:
            t = getattr(m, "type", "")
            if t == "system":
                c = m.content
                self._captured.append(c if isinstance(c, str) else "")
            elif t == "human":
                c = m.content
                self._humans.append(c if isinstance(c, str) else str(c))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.reply, id="cap"))


def seed_skill(home: Path, name: str, trigger: str = "both", body: str = "Skill body.") -> None:
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\ntrigger: {trigger}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# slash skills
# --------------------------------------------------------------------------- #
def test_slash_skill_substitution_reaches_model(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "summarize-notes", body="Summarize the given notes.")
    model = CapturingModel(reply="done")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/summarize-notes please condense my notes")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"
    assert model._humans, "model never received a human message"
    human = model._humans[-1]
    assert '<skill name="summarize-notes">' in human
    assert "Summarize the given notes." in human
    assert "User request: please condense my notes" in human


def test_filesystem_path_passthrough(create_session, ws_conv, isolated_home):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/tmp/foo is where the logs live")
        conv.recv_until("message.end", "error")
    assert model._humans[-1] == "/tmp/foo is where the logs live"


def test_model_invocable_skill_not_slash_callable(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "hidden", trigger="model-invocable")
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/hidden do it")
        conv.recv_until("message.end", "error")
    # trigger gating: the text passes through unmodified
    assert model._humans[-1] == "/hidden do it"


def test_help_short_circuits_without_model(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "summarize-notes")
    model = CapturingModel(reply="should never run")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/help")
        events = conv.recv_until("message.end", "error")
    assert event_names(events) == ["notice", "message.end"]
    assert not model._captured and not model._humans  # zero model calls
    notice = events_of(events, "notice")[0]
    assert "/help" in notice["message"]
    assert "/summarize-notes" in notice["message"]


# --------------------------------------------------------------------------- #
# /compact (async builtin — forced E3 compaction)
# --------------------------------------------------------------------------- #
def test_compact_empty_session_reports_nothing(create_session, ws_conv):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/compact")
        # 异步化（2026-11-11）：先收「已启动」notice + message.end（秒收口），
        # 后台 job 的结果 notice 随后广播到达。
        events = conv.recv_until("message.end", "error")
        assert event_names(events) == ["notice", "message.end"]
        assert "压缩已启动" in events_of(events, "notice")[0]["message"] or \
               "Compaction started" in events_of(events, "notice")[0]["message"]
        result = conv.recv_until("notice", "error")
    assert "no history messages to compact" in events_of(result, "notice")[0]["message"]
    assert not model._captured  # no graph turn, no model call


def test_compact_forces_summary_regardless_of_threshold(
    create_session, ws_conv, isolated_home
):
    # keep_turns=1 so a 2-turn history already has a compactable prefix; the
    # token threshold stays at its huge default — only the force path can fire.
    (isolated_home / "settings.json").write_text(
        json.dumps({"context": {"compact_keep_turns": 1}})
    )
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("第一个问题")
        conv.recv_until("message.end", "error")
        conv.invoke("第二个问题")
        conv.recv_until("message.end", "error")
        conv.invoke("/compact")
        # 异步化：message.end 秒到；context.compacting + 结果 notice 随后
        events = conv.recv_until("message.end", "error")
        assert "message.end" in event_names(events)
        tail = conv.recv_until("notice", "error")
    all_events = events + tail
    assert "context.compacting" in event_names(all_events)
    notices = events_of(all_events, "notice")
    result_notice = next(n for n in notices if "Context compacted" in n["message"])
    assert "tokens" in result_notice["message"]
    # 2 turn calls + 1 summarizer call (the summary SystemMessage is captured)
    assert len(model._captured) >= 3


# --------------------------------------------------------------------------- #
# @mentions
# --------------------------------------------------------------------------- #
def test_artifact_mention_attaches_file_without_dup_row(
    create_session, ws_conv, isolated_home, client
):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    f = isolated_home / "sales.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    art = art_store.add_artifact("default", "table", "sales.csv", str(f), sid)

    with ws_conv(sid) as conv:
        conv.invoke("帮我看看这个数据", mentions=[{"kind": "artifact", "id": art["id"]}])
        conv.recv_until("message.end", "error")
    # plan B1: attached files ride the turn-context message, not the system
    turn_ctx = turn_context_of(model)
    assert "<attached_files>" in turn_ctx
    assert "sales.csv" in turn_ctx
    # no duplicate artifact row was created by the resolution path
    rows = [a for a in art_store.list_artifacts("default") if a.get("name") == "sales.csv"]
    assert len(rows) == 1


def test_bound_workflow_injects_current_dsl(create_session, ws_conv):
    wf = wf_store.create_def(
        {
            "name": "cluster_checklist_review",
            "description": "review a cluster checklist",
            "dsl": {
                "entry": "parse",
                "nodes": [
                    {"id": "parse", "type": "step", "agent": "research", "goal": "read"},
                    {"id": "gate", "type": "human", "question": "确认继续？"},
                ],
                "edges": [{"from": "parse", "to": "gate"}],
            },
        }
    )
    model = CapturingModel(reply="这份流程有一个 human 确认节点 gate。")
    sid = create_session(model, agent_id="workflow-dev", workflow_id=wf["id"])
    with ws_conv(sid) as conv:
        conv.invoke("有Human 确认节点吗？")
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<bound_workflow>" in turn_ctx
    assert wf["id"] in turn_ctx
    assert "gate=human" in turn_ctx
    assert "cluster_checklist_review" in turn_ctx


def test_result_mention_injects_last_reply(create_session, ws_conv, isolated_home):
    """@result：目标会话最后 assistant 正文注入 <mentioned_result>（设计 §5.2）。
    目标走真 turn 产生回复（纯 e2e，不手写 checkpoint）。"""
    from ginno_runtime.session_links import stamp_link

    model = CapturingModel(reply="ok")
    main = create_session(model, agent_id="dev")
    target = create_session(CapturingModel(reply="第 8 期结论：候选 9 项"), agent_id="dev")
    stamp_link(main, target, "watching")
    # 目标先跑一轮，落一条真实 assistant 回复
    with ws_conv(target) as conv:
        conv.invoke("跑一期")
        conv.recv_until("message.end", "error")
    # 主会话 @result 引用
    with ws_conv(main) as conv:
        conv.invoke("结合结论挑 3 个", mentions=[{"kind": "result", "id": target}])
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_result>" in turn_ctx
    assert "第 8 期结论：候选 9 项" in turn_ctx


def test_memory_mention_retired(create_session, ws_conv, isolated_home):
    """@memory 已退役（2026-11-05 @ 范围整理）：world_state 每轮自动注入，
    mention 不再有专门通道——旧 payload 静默忽略且不注入重复段。"""
    paths.memory_index_path().write_text("用户偏好：先给结论再给细节。", encoding="utf-8")
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "memory"}])
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_memory>" not in turn_ctx


def test_memory_mention_empty_skipped(create_session, ws_conv):
    # isolated_home has no MEMORY.md content (or only boilerplate)
    paths.ensure_layout()
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "memory"}])
        conv.recv_until("message.end", "error")
    assert "<mentioned_memory>" not in model._captured[-1]


def test_agent_mention_overrides_routing(create_session, ws_conv, client):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "agent", "id": "research"}])
        events = conv.recv_until("message.end", "error")
    turn = events_of(events, "turn.start")[0]
    assert turn["agent_id"] == "research"
    # the session agent was persisted server-side
    meta = client.get(f"/api/sessions/{sid}").json()
    assert meta["agent_id"] == "research"


def test_mention_token_fallback_without_structured_list(create_session, ws_conv):
    """raw token（无 structured mentions）：@result:标题 在 linked 集合内命中。"""
    from ginno_runtime.session_links import stamp_link

    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    target = create_session(CapturingModel(reply="raw 结论"), agent_id="dev", title="雷达任务")
    stamp_link(sid, target, "watching")
    with ws_conv(target) as conv:
        conv.invoke("跑")
        conv.recv_until("message.end", "error")
    with ws_conv(sid) as conv:
        # raw API style: token in text, no structured mentions key
        conv.send({"type": "invoke", "message": "@result:雷达任务 总结下"})
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_result>" in turn_ctx
    assert "raw 结论" in turn_ctx


# --------------------------------------------------------------------------- #
# @session 投递（at-session-and-mention-scope-design §5.1）——ws 层短路：
# idle 目标后台 headless turn；working 目标 steer 队列；本会话不开 turn。
# --------------------------------------------------------------------------- #
def test_session_mention_delivers_to_idle_target(create_session, ws_conv):
    model_t = CapturingModel(reply="收到，开跑")
    main = create_session(CapturingModel(reply="ok"), agent_id="dev")
    target = create_session(model_t, agent_id="dev")
    from ginno_runtime.session_links import stamp_link
    stamp_link(main, target, "watching")
    import time as _t
    _t.sleep(0.05)

    with ws_conv(main) as conv:
        conv.invoke("去跑一期", mentions=[{"kind": "session", "id": target}])
        events = conv.recv_until("message.end", "error")
        # 来源会话：deliver.report + message.end，无 turn.start（不开本会话 turn）
        reports = events_of(events, "deliver.report")
        assert len(reports) == 1
        assert reports[0]["targets"][0]["state"] == "started"
        assert not events_of(events, "turn.start")
        # 目标后台 turn 落盘——必须在本 WS 生命周期内轮询（TestClient loop
        # 存活，spawn_bg 的任务才能被调度；连接断开后 loop 停转任务饿死）
        from ginno_runtime.checkpointer import FileCheckpointer
        cp = FileCheckpointer("default")
        msgs = []
        for _ in range(50):
            tup = cp.get_tuple({"configurable": {"thread_id": target}})
            # messages=None 是 turn 起步的合法中间态（channel 未初始化），
            # 轮询必须跳过而不是炸（2026-11-11 CI：decay_scan 挂启动维护
            # 后时序偏移，首轮即踩中该中间态）。
            msgs = ((tup.checkpoint.get("channel_values") or {}).get("messages") or []) if tup else []
            humans_now = [getattr(m, "content", "") for m in msgs]
            if any(h.startswith("[来自") for h in humans_now) and any(
                getattr(m, "type", "") == "ai" for m in msgs
            ):
                break
            _t.sleep(0.2)
    humans = [getattr(m, "content", "") for m in msgs]
    assert any(h.startswith("[来自") and "去跑一期" in h for h in humans)
    assert any(h == "收到，开跑" for h in humans)


def test_session_mention_busy_target_queues_steer(create_session, ws_conv):
    """working 目标：steer 队列（本用例用 _RUNNING_TURNS 模拟占用）。"""
    main = create_session(CapturingModel(reply="ok"), agent_id="dev")
    target = create_session(CapturingModel(reply="ok"), agent_id="dev")
    from ginno_runtime.session_links import stamp_link
    from ginno_runtime import server_shared as shared
    stamp_link(main, target, "watching")
    shared._RUNNING_TURNS[target] = "turn-occupied"
    try:
        with ws_conv(main) as conv:
            conv.invoke("插个队", mentions=[{"kind": "session", "id": target}])
            events = conv.recv_until("message.end", "error")
        reports = events_of(events, "deliver.report")
        assert reports[0]["targets"][0]["state"] == "queued"
        stash = shared._STEER_STASH.get(target) or []
        entry = next(e for e in stash if e.get("text", "").startswith("[来自"))
        assert "插个队" in entry["text"]
    finally:
        shared._RUNNING_TURNS.pop(target, None)
        # 投递留下的 stash（等目标 turn 吸收——生产语义正确）是本用例
        # 自己造的占用态产物，清掉防跨测试污染（stash 断言全 dict 空）。
        shared._STEER_STASH.pop(target, None)
