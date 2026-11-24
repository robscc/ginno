"""E2E: worker ask_user 中断透传（relay-question，2026-11-21 e2ce562）全链。

（文件名带 ``_flow`` 后缀：``tests/api/test_relay_question.py`` 是同名
单测，无 ``__init__.py`` 的 pytest 布局下两个同名文件在同一会话里
import 冲突——CI 全量必红。）

真 server + fake_model 驱动三条链路（unit 段 tests/api/test_relay_question.py
已覆盖函数面，这里钉跨 session 的真实管道）：

1. **全链**：worker parked 在 ask_user → note_parked_question 登记 → 订阅方
   session 收到 ``worker.pending_questions`` 事件 + ``<ginno_worker_question>``
   注入（开真 wake turn）→ 订阅方模型调 relay_answer → worker resume 完成
   → 终态按订阅语义回传（digest 信封）。
2. **幂等**：worker 侧用户直答（WS user_answer）先消费问题 → 订阅方晚到的
   relay_answer 命中墓碑返回 already_answered（ok:false / answered_by /
   answer / note），worker 的 checkpoint 文件与 WS 事件前后快照零变化。
3. **choice 自纠**：relay_answer 传不命中选项 → 工具报错列合法选项 → 模型
   重调命中 → worker 成功 resume。

测试脚手架约定（见 tests/e2e/test_ask_user_flow.py 与 test_subagent_e2e.py）：

* qid 凭证不可预知（uuid4），monkeypatch ``relay.new_qid`` 固定后，订阅方
  fake_model 的 tool_call 脚本才能携带 qid——ask_tools._new_qid 是惰性
  ``from ..worker_bus.relay import new_qid``，patch 模块属性即生效；
* 订阅方 model 的 ``scripts`` 在 create_session拿到 worker sid 之后回填
  （ScriptedChatModel.scripts 可变，agent_node 每次解析同一实例）；
* conftest 的 client 关闭 bypass_permissions，而 permission policy 默认
  ask——relay_answer 走用户显式 allow 规则（``permissions.allow``，
  与 web_search 同款合法产品路径），不 patch 权限节点本身；
* worker 侧快照用 checkpoint jsonl 文件 md5 + 消息 id 序列双口径；WS 侧
  用 ping/pong 之间的帧集合证明零新事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest
from conftest import event_names, events_of, script, script_tool_call

from ginno_runtime import jsonx as json
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.testing.fake_model import ScriptedChatModel
from ginno_runtime.worker_bus import relay
from ginno_runtime.worker_bus import watch as ww

pytestmark = pytest.mark.e2e

QID = "q_e2efixed001"
OPTS = ["Ginno 全局", "装到仓库"]


@pytest.fixture(autouse=True)
def _clean_relay_state():
    """relay 模块级登记表/墓碑 + resume 守卫是进程级内存——isolated_home 换
    GINNO_HOME 不会清它们（与 tests/api/test_relay_question.py 的同名 fixture
    同理：e2e 段残留会污染后跑的断言）。"""
    from ginno_runtime import server_shared

    relay._reset_state()
    yield
    relay._reset_state()
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def reorder_subscriber_checkpoint(monkeypatch):
    """Deliver a real interrupt before its checkpoint and writes finish landing."""
    from ginno_runtime.api.stream.stages import run

    subscriber = None
    release = asyncio.Event()
    writes_committed = asyncio.Event()
    agent_checkpoint_committed = asyncio.Event()
    aput = FileCheckpointer.aput
    aput_writes = FileCheckpointer.aput_writes
    chunked_stream = run.chunked_stream

    async def hold_checkpoint(self, config, checkpoint, metadata, new_versions):
        if config["configurable"]["thread_id"] == subscriber and metadata.get("step") == 1:
            await asyncio.wait_for(release.wait(), timeout=5)
            result = await aput(self, config, checkpoint, metadata, new_versions)
            agent_checkpoint_committed.set()
            return result
        return await aput(self, config, checkpoint, metadata, new_versions)

    async def record_writes(self, config, writes, task_id):
        conf = config["configurable"]
        channels = {channel for channel, _ in writes}
        if conf["thread_id"] == subscriber and "branch:to:tools" in channels:
            await asyncio.wait_for(agent_checkpoint_committed.wait(), timeout=5)
        await aput_writes(self, config, writes, task_id)
        if conf["thread_id"] == subscriber and "__interrupt__" in channels:
            assert not agent_checkpoint_committed.is_set()
            writes_committed.set()

    async def stream(ctx):
        async for mode, payload in chunked_stream(ctx):
            if ctx.session_id == subscriber and mode == "updates" and "__interrupt__" in payload:
                await asyncio.wait_for(writes_committed.wait(), timeout=5)
                try:
                    yield mode, payload
                finally:
                    release.set()
            else:
                yield mode, payload

    def target(sid):
        nonlocal subscriber
        subscriber = sid

    monkeypatch.setattr(FileCheckpointer, "aput", hold_checkpoint)
    monkeypatch.setattr(FileCheckpointer, "aput_writes", record_writes)
    monkeypatch.setattr(run, "chunked_stream", stream)
    return target


def _allow_relay_tool(isolated_home) -> None:
    """conftest client 关闭了 bypass_permissions；permission policy 默认 ask。
    给 relay_answer 写用户级 allow 规则（graph build 时 PermissionPolicy.
    from_settings 快照，须在 create_session 之前写）。"""
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text()) if sp.exists() else {}
    allow = s.setdefault("permissions", {}).setdefault("allow", [])
    if "relay_answer(*)" not in allow:
        allow.append("relay_answer(*)")
    sp.write_text(json.dumps(s))


def _ask_call(call_id: str = "call_ask_1") -> dict:
    return script_tool_call(
        "ask_user",
        {"question": "「迁移配置」装到哪？", "options": OPTS, "header": "选择安装位置"},
        call_id,
    )


def _relay_call(worker: str, choice: str, call_id: str, reason: str = "用户明确选择了仓库位置") -> dict:
    return script_tool_call(
        "relay_answer",
        {"worker_session": worker, "qid": QID, "choice": choice, "mapping_reason": reason},
        call_id,
    )


def _msgs(cp: FileCheckpointer, sid: str) -> list:
    tup = cp.get_tuple({"configurable": {"thread_id": sid}})
    return (tup.checkpoint.get("channel_values") or {}).get("messages") or [] if tup else []


def _wait_for(cp, sid, pred, timeout: float = 15.0):
    """轮询 checkpoint 直到 pred(messages) 真值（CI 慢机器竞态防御，
    见 tests/e2e/test_ask_user_flow.py 的 _wait_question_blocks 先例）。"""
    deadline = time.time() + timeout
    out = pred(_msgs(cp, sid))
    while not out and time.time() < deadline:
        time.sleep(0.2)
        out = pred(_msgs(cp, sid))
    return out


def _relay_tool_results(cp, sid: str) -> list[str]:
    """sub checkpoint 里全部 relay_answer ToolMessage 的 content（JSON 串，
    checkpoint 侧未截断——WS 的 tool.end 帧会被 _truncate_for_ws 截）。"""
    return [
        str(m.content)
        for m in _msgs(cp, sid)
        if getattr(m, "name", "") == "relay_answer"
    ]


def _worker_env_messages(cp, sid: str, kind: str) -> list:
    return [
        m
        for m in _msgs(cp, sid)
        if ((getattr(m, "additional_kwargs", None) or {}).get("ginno_worker_msg") or {}).get("kind") == kind
    ]


def _msg_digest(cp, sid: str) -> list:
    """checkpoint 消息指纹：消息 id + 类型（数量/顺序变化即变）。"""
    return [(getattr(m, "id", None), getattr(m, "type", None)) for m in _msgs(cp, sid)]


def _ckpt_md5(sid: str) -> str:
    from ginno_runtime import paths

    p = paths.project_sessions_dir("default") / f"{sid}.jsonl"
    if not p.exists():
        return "<missing>"
    return hashlib.md5(p.read_bytes()).hexdigest()


def _stable_snapshot(cp, sid: str, timeout: float = 5.0):
    """等 checkpoint 稳定（turn settle 后异步落盘完成）再拍照。"""
    last = (_msg_digest(cp, sid), _ckpt_md5(sid))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        cur = (_msg_digest(cp, sid), _ckpt_md5(sid))
        if cur == last:
            return cur
        last = cur
    return last


def _ask_settlement(rest: list[dict]) -> dict:
    ends = [e for e in events_of(rest, "tool.end") if e.get("id") == "call_ask_1"]
    assert ends, f"no ask_user tool.end in {[e.get('event') for e in rest]}"
    return json.loads(ends[-1]["content"])


# --------------------------------------------------------------------------- #
# 1) 全链：worker 弹问 → 订阅方注入 → relay_answer → resume → digest 回传
# --------------------------------------------------------------------------- #


def test_full_relay_loop(client, create_session, ws_conv, isolated_home, monkeypatch):
    monkeypatch.setattr(relay, "new_qid", lambda: QID)
    _allow_relay_tool(isolated_home)
    cp = FileCheckpointer("default")

    sub_model = ScriptedChatModel(scripts=[])
    worker_model = ScriptedChatModel(
        scripts=[script(tool_calls=[_ask_call()]), script(text="按仓库安装完成。")]
    )
    sub = create_session(sub_model, title="订阅方")
    worker = create_session(worker_model, title="迁移工人")
    # worker sid 后回填订阅方脚本：第一跳 relay_answer，第二跳收尾文本。
    sub_model.scripts = [
        script(tool_calls=[_relay_call(worker, "装到仓库", "call_relay_1")]),
        script(text="已转达用户的选择。"),
    ]
    # 订阅关系走真实 watch_worker API（digest 语义——main 会话无终态）。
    r = client.portal.call(ww.watch_worker, sub, worker, "digest")
    assert r.get("ok") is True and r.get("watched") is True, r

    with ws_conv(worker) as wconv, ws_conv(sub) as sconv:
        wconv.invoke("跑迁移")
        first = wconv.recv_until("user.question", "message.end", "error")
        # turn park 在问句上（先于 message.end）
        assert "user.question" in event_names(first)
        q = events_of(first, "user.question")[0]
        assert q["qid"] == QID  # 事件帧带 qid（凭证链第二层入口）
        assert q["options"] == OPTS
        # 登记已发生（note_parked 在 park 流程内 await 完成）
        assert relay.find_pending(QID) is not None
        ent = relay.find_pending(QID)
        assert ent["worker_sid"] == worker and sub in ent["subscribers"]

        # 订阅方 session 收到 pending_questions digest 事件（②字段契约）
        sevs = sconv.recv_until("worker.pending_questions", "error")
        rows = events_of(sevs, "worker.pending_questions")
        assert rows, f"no worker.pending_questions in {event_names(sevs)}"
        row = rows[0]["questions"][0]
        assert row["qid"] == QID and row["worker_sid"] == worker
        assert row["options"] == OPTS and row["worker_title"] == "迁移工人"

        # 订阅方 wake turn 跑完 relay_answer（checkpoint 全量 JSON，不经 WS 截断）。
        # 必须仍在同一 worker 连接内等 resume 终态：断连窗口内 message.end
        # 只会打 turn_client_gone 日志，新连接收不到（resume 事件不重放）。
        outs = _wait_for(cp, sub, lambda ms: _relay_tool_results(cp, sub) or None)
        assert outs, "sub never ran relay_answer"
        rout = json.loads(outs[0])
        assert rout["ok"] is True and rout["resumed"] is True
        assert rout["qid"] == QID and rout["worker_session"] == worker
        assert rout["relayed_by"] == sub

        # worker resume 完成：终段事件 + ask_user 按转达选项结算
        rest = wconv.recv_until("message.end", "error")
        assert not events_of(rest, "error"), rest
        out = _ask_settlement(rest)
        assert out["ok"] is True and out["answer"] == "装到仓库" and out["option_index"] == 1

        # 登记即销 + 墓碑溯源（代答人 = 订阅方）
        assert relay.find_pending(QID) is None
        tb = relay.tombstone_of(QID)
        assert tb and tb["answered_by"] == sub and tb.get("relayed_by") == sub

    # 注入信封落订阅方 checkpoint（ginno_worker_question + qid）
    env = _wait_for(
        cp, sub,
        lambda ms: [
            m for m in ms
            if ((getattr(m, "additional_kwargs", None) or {}).get("ginno_worker_msg") or {}).get("qid") == QID
        ],
    )
    assert env, "no ginno_worker_question envelope in sub checkpoint"
    body = str(env[0].content)
    assert "<ginno_worker_question" in body and worker in body and QID in body
    assert "装到仓库" in body  # 模板携带原选项

    # 终态按订阅语义回传（digest 订阅 → digest 信封，含 worker 产出摘要）
    dg = _wait_for(
        cp, sub,
        lambda ms: [
            m for m in ms
            if ((getattr(m, "additional_kwargs", None) or {}).get("ginno_worker_msg") or {}).get("kind") == "digest"
            or 'kind="digest"' in str(getattr(m, "content", ""))
        ],
    )
    assert dg, "no digest envelope relayed back to subscriber"
    assert "按仓库安装完成" in str(dg[0].content)


# --------------------------------------------------------------------------- #
# 2) 幂等：worker 先答 → 晚到的 relay_answer 纯只读（already_answered 四不）
# --------------------------------------------------------------------------- #


def test_worker_answered_first_late_relay_is_readonly(
    client, create_session, ws_conv, isolated_home, monkeypatch
):
    """幂等：worker 用户直答在前，订阅方的 relay_answer 晚到 → already_answered。

    「晚到」用真实窗口编排：本用例**不**给 relay_answer 写 allow 规则——
    conftest 关闭 bypass 后 permission policy 默认 ask，订阅方模型发出
    relay_answer 调用即 park 在批准提示上（bind 发生在 superstep 前，
    pending 登记仍在）。批准等待期间 worker 的用户直答并即销登记、写墓碑；
    随后批准放行，tools_node 执行的已是「登记即销后」的调用——relay_answer
    命中墓碑返回 already_answered，纯只读四不（不 resume / 不写 checkpoint /
    不广播 / 不动注册表）。
    """
    monkeypatch.setattr(relay, "new_qid", lambda: QID)
    # 2026-11-21 起 startup 会把 relay_answer seed 进 allow（用户拍板默认
    # allow）——本用例的编排依赖「默认 ask 弹批准」窗口，须显式写 ask 规则
    # 压过（policy 优先级 deny>ask>allow）。不写则 subscription 的 relay_answer
    # 直接放行、等不到 permission.request → recv_until 挂死。
    sp = isolated_home / "settings.json"
    s0 = json.loads(sp.read_text()) if sp.exists() else {}
    asks = s0.setdefault("permissions", {}).setdefault("ask", [])
    if "relay_answer(*)" not in asks:
        asks.append("relay_answer(*)")
    sp.write_text(json.dumps(s0))
    cp = FileCheckpointer("default")

    sub_model = ScriptedChatModel(scripts=[])
    worker_model = ScriptedChatModel(
        scripts=[script(tool_calls=[_ask_call()]), script(text="用户直答，按全局装。")]
    )
    sub = create_session(sub_model, title="订阅方")
    worker = create_session(worker_model, title="迁移工人")
    sub_model.scripts = [
        # wake turn（park 注入）第一跳即 relay_answer → park 在批准上
        script(tool_calls=[_relay_call(worker, "装到仓库", "call_relay_late")]),
        script(text="对方用户已直答，无需再转达。"),
    ]
    client.portal.call(ww.watch_worker, sub, worker, "digest")

    with ws_conv(worker) as wconv, ws_conv(sub) as sconv:
        wconv.invoke("跑迁移")
        first = wconv.recv_until("user.question", "message.end", "error")
        assert "user.question" in event_names(first)

        # 订阅方 wake turn：relay_answer 触发默认 ask 权限 → park 在批准上
        pevs = sconv.recv_until("permission.request", "error")
        preqs = events_of(pevs, "permission.request")
        assert preqs, f"no permission.request in {event_names(pevs)}"
        assert preqs[0]["tool"] == "relay_answer"

        # 批准等待期间：worker 侧用户直答（与既有 test_ask_user_flow 同协议）
        wconv.respond_answer("Ginno 全局", option_index=0)
        rest = wconv.recv_until("message.end", "error")
        assert not events_of(rest, "error"), rest
        out = _ask_settlement(rest)
        assert out["answer"] == "Ginno 全局" and out["option_index"] == 0

        # 直答即销：登记空、墓碑 answered_by=user（resume_question 统一收口）
        assert relay.find_pending(QID) is None
        tb = relay.tombstone_of(QID)
        assert tb and tb["answered_by"] == "user" and tb["answer"] == "Ginno 全局"

        # 前快照：WS 队列清空 + checkpoint 稳定
        wconv.send({"type": "ping"})
        drained = wconv.recv_until("pong", "error")
        assert not events_of(drained, "error")
        snap = _stable_snapshot(cp, worker)

        # 批准放行 → 晚到的 relay_answer 执行 → 命中墓碑
        sconv.respond_permission("allow")
        outs = _wait_for(cp, sub, lambda ms: _relay_tool_results(cp, sub) or None)
        assert outs, "sub never ran the late relay_answer"
        rout = json.loads(outs[0])
        # already_answered shape（goal ②）：ok:false / answered_by / answer / note
        assert rout["ok"] is False and rout["already_answered"] is True
        assert rout["answered_by"] == "user"
        assert rout["answer"] == "Ginno 全局"
        assert str(rout.get("note") or "") != ""

        # 后快照：worker checkpoint（消息指纹 + jsonl 文件 md5）零变化
        assert _msg_digest(cp, worker) == snap[0]
        assert _ckpt_md5(worker) == snap[1]

        # 事件零变化：relay 之后 worker WS 只有 pong，无任何新帧
        wconv.send({"type": "ping"})
        after = wconv.recv_until("pong", "error")
        assert [e.get("event") for e in after] == ["pong"], after


@pytest.mark.parametrize("restart", [False, True], ids=["live", "legacy-restart"])
def test_worker_choice_resumes_subscriber_relay_card(
    client, create_session, ws_conv, patch_build_model, restart, reorder_subscriber_checkpoint,
):
    cp = FileCheckpointer("default")
    sub_call = "call_subscriber_ask"
    sub_model = ScriptedChatModel(scripts=[
        script(tool_calls=[script_tool_call(
            "ask_user", {"question": "这次迁移选哪个位置？", "options": OPTS[::-1]}, sub_call,
        )]),
        script(text="已收到订阅会话选的位置。"),
    ])
    sub = create_session(sub_model)
    if not restart:
        reorder_subscriber_checkpoint(sub)
    worker = create_session([
        script(tool_calls=[_ask_call()]), script(text="已按选择完成。"),
    ])
    assert client.portal.call(ww.watch_worker, sub, worker, "digest")["ok"]

    with ws_conv(worker) as wconv, ws_conv(sub) as sconv:
        wconv.invoke("跑迁移")
        original = events_of(wconv.recv_until("user.question", "error"), "user.question")[0]
        forwarded = events_of(sconv.recv_until("user.question", "error"), "user.question")[0]
        assert original["qid"] != forwarded["qid"]
        assert forwarded["id"] == sub_call
        assert forwarded["options"] == OPTS[::-1]

        if restart:
            from ginno_runtime import server_shared as shared
            from ginno_runtime.api.stream.resume import resume_pending_interrupt

            def answer_with_old_runtime():
                assert resume_pending_interrupt(worker, {
                    "kind": "user_answer", "answer": OPTS[0], "option_index": 0,
                })
                relay.mark_answered(original["qid"], "user", OPTS[0])
                # Recreate the 1ff55791 tombstone shape on disk.
                relay.tombstone_of(original["qid"]).pop("subscribers", None)
                relay._persist_unlocked()

            client.portal.call(answer_with_old_runtime)
            assert _ask_settlement(wconv.recv_until("message.end", "error"))["answer"] == OPTS[0]

            async def restart_relay():
                async with shared._turn_lock(sub):
                    shared._SESSIONS.pop(sub)
                    shared._PENDING_KIND.pop(sub)
                    shared._PENDING_RESUME.remove(sub)
                    shared._RUNNING_TURNS.pop(sub)
                    relay._reset_state()
                    relay.reconcile_relay()

            patch_build_model(sub_model)
            client.portal.call(restart_relay)
        else:
            # Only answer the original card, with the order reversed in the relay.
            wconv.respond_answer(OPTS[0], option_index=0)
            assert _ask_settlement(wconv.recv_until("message.end", "error"))["answer"] == OPTS[0]

        receipts = _wait_for(cp, sub, lambda messages: [
            m for m in messages if getattr(m, "tool_call_id", "") == sub_call
        ], timeout=8)
        assert receipts and len(receipts) == 1
        answered = json.loads(receipts[0].content)
        assert answered["source"] == "option"
        assert answered["answer"] == OPTS[0]
        assert answered["option_index"] == 1

        frames = sconv.recv_until("message.end", "error")
        assert not events_of(frames, "error")
        assert not events_of(frames, "user.question")
        settled = [e for e in events_of(frames, "tool.end") if e.get("id") == sub_call]
        assert len(settled) == 1 and settled[0]["turn_id"] == forwarded["turn_id"]

        history = client.get(f"/api/sessions/{sub}/history").json()["messages"]
        cards = [b for m in history for b in m["blocks"] if b.get("id") == sub_call]
        assert len(cards) == 1
        assert cards[0]["status"] == "answered"
        assert cards[0]["answer"] == OPTS[0] and cards[0]["optionIndex"] == 1


def test_worker_choice_before_subscriber_parks(
    client, create_session, ws_conv, reorder_subscriber_checkpoint,
):
    release = asyncio.Event()

    class HeldModel(ScriptedChatModel):
        async def _astream(self, *args, **kwargs):
            await release.wait()
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    sub_call = "call_late_subscriber_ask"
    sub = create_session(HeldModel(scripts=[
        script(tool_calls=[script_tool_call(
            "ask_user", {"question": "选哪个位置？", "options": OPTS}, sub_call,
        )]),
        script(text="已同步。"),
    ]))
    worker = create_session([
        script(tool_calls=[_ask_call()]), script(text="已作答。"),
    ])
    assert client.portal.call(ww.watch_worker, sub, worker, "digest")["ok"]

    reorder_subscriber_checkpoint(sub)

    with ws_conv(worker) as wconv, ws_conv(sub) as sconv:
        wconv.invoke("跑迁移")
        assert events_of(wconv.recv_until("user.question", "error"), "user.question")
        assert events_of(sconv.recv_until("turn.start", "error"), "turn.start")
        wconv.respond_answer(OPTS[1], option_index=1)
        assert _ask_settlement(wconv.recv_until("message.end", "error"))["answer"] == OPTS[1]
        client.portal.call(release.set)

        frames = sconv.recv_until("message.end", "user.question", "error")
        assert frames[-1]["event"] == "message.end"
        ends = [e for e in events_of(frames, "tool.end") if e.get("id") == sub_call]
        assert len(ends) == 1 and json.loads(ends[0]["content"])["answer"] == OPTS[1]
        history = client.get(f"/api/sessions/{sub}/history").json()["messages"]
        cards = [b for m in history for b in m["blocks"] if b.get("id") == sub_call]
        assert len(cards) == 1 and cards[0]["status"] == "answered"


# --------------------------------------------------------------------------- #
# 3) choice 自纠：不命中 → 报错列合法选项 → 重调命中 → resume 成功
# --------------------------------------------------------------------------- #


def test_relay_choice_self_corrects(client, create_session, ws_conv, isolated_home, monkeypatch):
    monkeypatch.setattr(relay, "new_qid", lambda: QID)
    _allow_relay_tool(isolated_home)
    cp = FileCheckpointer("default")

    sub_model = ScriptedChatModel(scripts=[])
    worker_model = ScriptedChatModel(
        scripts=[script(tool_calls=[_ask_call()]), script(text="自纠后的答案已收到。")]
    )
    sub = create_session(sub_model, title="订阅方")
    worker = create_session(worker_model, title="迁移工人")
    sub_model.scripts = [
        # 第一跳：不命中的自由文本（mapping_reason 已给——确保报错来自 choice）
        script(tool_calls=[_relay_call(worker, "随便装哪", "call_relay_bad", reason="乱猜的映射")]),
        # 第二跳：映射回原选项
        script(tool_calls=[_relay_call(worker, "装到仓库", "call_relay_good")]),
        script(text="自纠后转达成功。"),
    ]
    client.portal.call(ww.watch_worker, sub, worker, "digest")

    with ws_conv(worker) as wconv:
        wconv.invoke("跑迁移")
        first = wconv.recv_until("user.question", "message.end", "error")
        assert "user.question" in event_names(first)

        # 第一跳失败先落地（bounded 轮询，不悬在 WS recv 上）
        outs = _wait_for(cp, sub, lambda ms: _relay_tool_results(cp, sub) or None)
        assert outs, "sub never ran relay_answer"
        bad = json.loads(outs[0])
        assert bad["ok"] is False
        # 报错列合法选项（LLM 自纠所需信号），locale 无关：选项原文必在
        assert "装到仓库" in bad["error"] and "Ginno 全局" in bad["error"]
        # 未消耗：worker 仍 parked（第二跳成功由下方 resume 证明）

        # 第二跳命中 → worker resume 完成
        outs2 = _wait_for(
            cp, sub,
            lambda ms: _relay_tool_results(cp, sub)
            if len(_relay_tool_results(cp, sub)) >= 2 else None,
        )
        assert outs2 and len(outs2) >= 2, f"self-correction never landed: {outs}"
        good = json.loads(outs2[1])
        assert good["ok"] is True and good["resumed"] is True and good["qid"] == QID

        rest = wconv.recv_until("message.end", "error")
        assert not events_of(rest, "error"), rest
        out = _ask_settlement(rest)
        assert out["answer"] == "装到仓库" and out["option_index"] == 1

    assert relay.find_pending(QID) is None
    tb = relay.tombstone_of(QID)
    assert tb and tb["answered_by"] == sub
