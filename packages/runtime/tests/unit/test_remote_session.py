"""远端会话 R1（remote-session-design.md）：路由 / fail-fast / 持久化 / 恢复。

核心契约：会话挂 remote_peer → turn 在远端执行，本地不跑图。
A（peer 当子 agent）与 B（会话级归属远端）共用这条路径。
"""

from __future__ import annotations

import pytest

from ginno_runtime.api.stream import remote_turn as rt


def test_remote_peer_of_reads_session_field():
    assert rt.remote_peer_of({}) == ""
    assert rt.remote_peer_of({"remote_peer": None}) == ""
    assert rt.remote_peer_of({"remote_peer": "  "}) == ""
    assert rt.remote_peer_of({"remote_peer": " vps "}) == "vps"


def test_missing_peer_fails_loud(monkeypatch):
    """peer 不存在 → 抛错，**绝不静默回退本地**（定案 #7）。"""
    from ginno_runtime.a2a import peers as peers_mod

    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: None)
    with pytest.raises(rt.RemoteTurnError, match="不存在"):
        rt._resolve_peer("ghost")


def test_disabled_peer_fails_loud(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://h/a2a", enabled=False)
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    with pytest.raises(rt.RemoteTurnError, match="已禁用"):
        rt._resolve_peer("vps")


def test_peer_without_endpoint_fails_loud(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="")
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    with pytest.raises(rt.RemoteTurnError, match="endpoint"):
        rt._resolve_peer("vps")


def test_task_text_extracts_status_message():
    from ginno_runtime.a2a.models import Message, Part, Task, TaskStatus, TaskState

    t = Task(id="t1", context_id="c1", status=TaskStatus(
        state=TaskState.COMPLETED,
        message=Message(role="agent", parts=[Part(text="远端回复"), Part(text="第二行")]),
    ))
    assert rt._task_text(t) == "远端回复\n第二行"


def test_task_text_none_safe():
    assert rt._task_text(None) == ""


def test_task_artifacts_uses_content_part_not_summary():
    """P1-1 契约：part[0] 是路径摘要，part[1:] 才是内容。"""
    from ginno_runtime.a2a.models import Artifact, Message, Part, Task, TaskStatus, TaskState

    t = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.COMPLETED),
             artifacts=[Artifact(
                 artifact_id="a1", name="r.md",
                 parts=[Part(text="path: /work/r.md\nr.md (text, 10 bytes)"),
                        Part(text="# 正文\n中文内容")],
             )])
    arts = rt._task_artifacts(t)
    assert len(arts) == 1
    assert "中文内容" in arts[0]["content"]
    assert "path: /work/r.md" in arts[0]["content"]


def test_task_artifacts_none_safe():
    assert rt._task_artifacts(None) == []


async def test_run_stream_routes_to_remote_and_skips_graph(monkeypatch):
    """挂 remote_peer 的会话：_run_stream 必须走远端，**绝不碰本地图**。"""
    from ginno_runtime.api.stream import turn as turn_mod

    called = {}

    async def fake_remote(session, text, turn_id=None, *, agent_id=None):
        called["remote"] = (session.get("session_id") or session.get("id"), text, turn_id, agent_id)
        return {"ok": True, "text": "远端答复", "state": "completed", "task_id": "x", "error": ""}

    monkeypatch.setattr(rt, "run_remote_turn", fake_remote)
    # 图被调用即失败——证明本地路径确实没走
    class _BoomGraph:
        def __getattr__(self, n):
            raise AssertionError(f"远端会话不得触碰本地图（访问了 {n}）")

    sess = {"session_id": "s1", "remote_peer": "vps", "graph": _BoomGraph(), "project_slug": "default"}
    cfg = {"configurable": {"turn_id": "tid-1", "thread_id": "s1"}}
    await turn_mod._run_stream(None, sess["graph"], cfg, "你好", sess, "agent-x")
    assert called["remote"] == ("s1", "你好", "tid-1", "agent-x")


async def test_run_stream_local_session_untouched(monkeypatch):
    """未挂 remote_peer 的会话不得进远端路径（桌面零影响）。"""
    from ginno_runtime.api.stream import turn as turn_mod

    called = []

    async def fake_remote(*a, **k):
        called.append(a)
        return {}

    monkeypatch.setattr(rt, "run_remote_turn", fake_remote)
    sess = {"id": "s2", "graph": None, "project_slug": "default"}
    cfg = {"configurable": {"turn_id": "t2", "thread_id": "s2"}}
    # 本地图为 None 会在后续报错，但**远端路径必须没被调用**
    try:
        await turn_mod._run_stream(None, None, cfg, "hi", sess, None)
    except Exception:
        pass
    assert called == [], "本地会话不得走远端路径"


async def test_remote_turn_network_failure_reports_not_silent(monkeypatch):
    """对端不可达 → 发 error notice + done，绝不静默回退本地。"""
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://127.0.0.1:1/a2a",
                               task_timeout_s=5)
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    events = []

    async def fake_push(sid, ev, data, tid=None):
        events.append((ev, data))

    monkeypatch.setattr(rt, "_push_session_event", fake_push)
    persisted = []

    async def fake_persist(session, user_text, reply):
        persisted.append(reply)

    monkeypatch.setattr(rt, "_persist_exchange", fake_persist)
    sess = {"id": "s3", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "干活", "tid-3")
    assert res["ok"] is False
    assert res["error"], "必须带明确错误"
    evs = [e for e, _ in events]
    assert "turn.start" in evs and "done" in evs, "失败也要发终态，前端不能卡 streaming"
    assert any(e == "notice" and d.get("kind") == "error" for e, d in events)
    assert persisted and "远端执行失败" in persisted[0]


def test_create_session_request_accepts_remote_peer():
    from ginno_runtime.api.sessions import CreateSessionRequest

    r = CreateSessionRequest(project_slug="default", workspace="/w", remote_peer="vps")
    assert r.remote_peer == "vps"
    r2 = CreateSessionRequest(project_slug="default", workspace="/w")
    assert r2.remote_peer is None


# ── R2：peer 当子 agent（A）─────────────────────────────────────────────────


async def test_create_subagent_with_unknown_peer_fails_fast(monkeypatch):
    """peer 不存在 → ok:False，**绝不静默改成本地子代理**。"""
    from ginno_runtime.subagent_scheduler import spawn as sp
    from ginno_runtime.a2a import peers as peers_mod

    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: None)
    res = await sp.create_subagent("parent-1", "干活", peer="ghost")
    assert res["ok"] is False
    assert "ghost" in res["error"]


async def test_create_subagent_peer_fork_mutually_exclusive():
    """fork 依赖本地 checkpoint 重放，远端图状态不在本机（定案 #12）。"""
    from ginno_runtime.subagent_scheduler import spawn as sp

    res = await sp.create_subagent("parent-1", "干活", peer="vps", fork=True)
    assert res["ok"] is False
    assert "fork" in res["error"].lower()


async def test_create_subagent_disabled_peer_fails_fast(monkeypatch):
    from ginno_runtime.subagent_scheduler import spawn as sp
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://h/a2a", enabled=False)
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    res = await sp.create_subagent("parent-1", "干活", peer="vps")
    assert res["ok"] is False


async def test_peers_api_masks_token(monkeypatch):
    """/api/a2a/peers 绝不回 token 明文。"""
    from ginno_runtime.api import connectors as capi
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", name="VPS", endpoint="http://h/a2a",
                               token="SUPER-SECRET-TOKEN")
    monkeypatch.setattr(peers_mod.PeerStore, "list_all", lambda self: [rec])
    res = await capi.list_a2a_peers()
    assert res["ok"] is True
    assert len(res["peers"]) == 1
    p = res["peers"][0]
    assert p["slug"] == "vps" and p["name"] == "VPS"
    blob = str(res)
    assert "SUPER-SECRET-TOKEN" not in blob, "token 明文不得出现在响应里"
    assert "token" not in p, "选择器不需要 token 字段"


async def test_peers_api_read_failure_returns_empty(monkeypatch):
    from ginno_runtime.api import connectors as capi
    from ginno_runtime.a2a import peers as peers_mod

    def boom(self):
        raise OSError("disk gone")

    monkeypatch.setattr(peers_mod.PeerStore, "list_all", boom)
    res = await capi.list_a2a_peers()
    assert res == {"ok": True, "peers": []}


def test_spawn_subagent_tool_exposes_peer():
    """工具面必须有 peer 参数，否则模型/用户无法显式指派远端。"""
    import inspect

    from ginno_runtime.tools import subagent as sa

    src = inspect.getsource(sa)
    assert "peer: str" in src
    assert "peer=peer" in src


# ── 2026-11-17：真跑执行器的成功路（补假绿缺口）──────────────────────────────
#
# 事故：远端会话在真机上每轮必崩
#   TypeError: ContextMap.get_or_none() takes 2 positional arguments but 3 were given
# 而 18 例既有测试全绿。两层假绿：
#   ① 路由测试 monkeypatch.setattr(rt, "run_remote_turn", fake_remote)
#      ——把整个执行器桩掉，真实代码路径从未跑过；
#   ② 唯一真调执行器的 test_remote_turn_network_failure_reports_not_silent
#      断言的是 `ok is False` + "远端执行失败"——**TypeError 也满足这两条**，
#      自己的代码崩了被当成「对端不可达」，测试照绿。
# 教训：失败路断言必须能区分「对端故障」与「自身 bug」；成功路不可缺席。


class TestRemoteTurnSuccessPath:
    @pytest.fixture
    def _wire(self, isolated_home, monkeypatch):
        """真 ContextMap（隔离 home）+ 假 A2A client（成功返回）。

        **不桩 run_remote_turn 本身**——那正是漏掉 bug 的原因。
        """
        from types import SimpleNamespace

        from ginno_runtime.a2a import peers as peers_mod

        rec = peers_mod.PeerRecord(
            slug="vps", endpoint="http://127.0.0.1:18790/a2a/CAP",
        )
        monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
        events: list = []

        async def fake_push(sid, ev, data, tid=None):
            events.append((ev, data))

        monkeypatch.setattr(rt, "_push_session_event", fake_push)
        persisted: list = []

        async def fake_persist(session, user_text, reply):
            persisted.append(reply)

        monkeypatch.setattr(rt, "_persist_exchange", fake_persist)

        sent: list = []

        class FakeClient:
            async def send_message(self, text, *, context_id=None,
                                   reference_task_ids=None):
                sent.append({"text": text, "context_id": context_id,
                             "refs": reference_task_ids})
                return SimpleNamespace(
                    id="task-1",
                    context_id="ctx-remote-1",
                    status=SimpleNamespace(
                        state="completed",
                        message=SimpleNamespace(
                            parts=[SimpleNamespace(text="hostname=vps pwd=/work")],
                        ),
                    ),
                    artifacts=[],
                )

            async def aclose(self):
                pass

        monkeypatch.setattr(rt, "_client", lambda rec: FakeClient())
        return SimpleNamespace(events=events, persisted=persisted, sent=sent)

    async def test_success_path_reaches_peer_and_returns_text(self, _wire):
        """成功路必须真跑通——这条才能抓住执行器内部的 TypeError。"""
        sess = {"id": "s-ok", "remote_peer": "vps", "graph": None}
        res = await rt.run_remote_turn(sess, "报告 hostname 和 pwd", "tid-ok")
        assert res["ok"] is True, f"远端成功路必须通，实际 error={res.get('error')!r}"
        assert "hostname=vps" in res["text"]
        assert _wire.sent and _wire.sent[0]["text"] == "报告 hostname 和 pwd"
        evs = [e for e, _ in _wire.events]
        assert "turn.start" in evs and "done" in evs
        assert not any(
            e == "notice" and d.get("kind") == "error" for e, d in _wire.events
        ), "成功路不得发 error notice"

    async def test_second_turn_reuses_remote_context(self, _wire):
        """第二轮必须带上首轮拿到的 contextId（云端续作的基础）。"""
        sess = {"id": "s-ctx", "remote_peer": "vps", "graph": None}
        r1 = await rt.run_remote_turn(sess, "第一轮", "t1")
        assert r1["ok"] is True, r1.get("error")
        r2 = await rt.run_remote_turn(sess, "第二轮", "t2")
        assert r2["ok"] is True, r2.get("error")
        assert _wire.sent[1]["context_id"] == "ctx-remote-1"

    async def test_context_lookup_uses_single_key(self, _wire):
        """ContextMap.get_or_none 只收单个已构造 key——直接钉住签名契约。"""
        from ginno_runtime.a2a.contexts import ContextMap, _key

        cm = ContextMap()
        # 两参调用必须 TypeError（契约本身），单参必须可用
        with pytest.raises(TypeError):
            cm.get_or_none("s", "vps")  # type: ignore[call-arg]
        assert cm.get_or_none(_key("s", "vps")) is None


async def test_network_failure_error_is_not_our_own_bug(monkeypatch, isolated_home):
    """加固既有失败路：错误必须是**对端故障**，不能是自身代码的 TypeError。

    原断言 `ok is False` + "远端执行失败" 无法区分两者，导致执行器内部
    TypeError 被当成网络故障、测试假绿（2026-11-17 实锤）。
    """
    from ginno_runtime.a2a import peers as peers_mod

    rec = peers_mod.PeerRecord(slug="vps", endpoint="http://127.0.0.1:1/a2a",
                               task_timeout_s=5)
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)

    async def fake_push(sid, ev, data, tid=None):
        pass

    monkeypatch.setattr(rt, "_push_session_event", fake_push)

    async def fake_persist(session, user_text, reply):
        pass

    monkeypatch.setattr(rt, "_persist_exchange", fake_persist)
    sess = {"id": "s-net", "remote_peer": "vps", "graph": None}
    res = await rt.run_remote_turn(sess, "干活", "tid-net")
    assert res["ok"] is False
    err = str(res.get("error") or "")
    assert "TypeError" not in err, f"自身代码 bug 不得伪装成网络故障：{err}"
    assert "get_or_none" not in err, f"自身代码 bug：{err}"


# ---- session 载荷 remote_peer_name（i18n {name} 错位修复，2026-11-18）--------
# 前端模型 chip/远端徽标此前靠 useA2aPeers 异步拉取再按 slug 找 name，落定前
# {name} 占位符闪 slug（test_packaged_ui_llm_events 的 model_chip 用例因此
# flaky）。修复 = session 载荷在后端真源就地解析显示名，找不到才回 slug。


def test_remote_peer_name_resolves_from_store(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod
    from ginno_runtime.api import sessions as s

    rec = peers_mod.PeerRecord(
        slug="vps", name="VPS 节点", endpoint="http://h/a2a"
    )
    monkeypatch.setattr(
        peers_mod.PeerStore, "find", lambda self, slug: rec if slug == "vps" else None
    )
    assert s._remote_peer_name("vps") == "VPS 节点"
    # 解析不到 → 回 slug（「归属事实不因列表缺失而消失」的既有语义，收归后端单点）
    assert s._remote_peer_name("ghost") == "ghost"
    # 非远端 → None（载荷不加键）
    assert s._remote_peer_name(None) is None
    assert s._remote_peer_name("") is None


def test_remote_peer_name_empty_name_falls_back_to_slug(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod
    from ginno_runtime.api import sessions as s

    rec = peers_mod.PeerRecord(slug="vps", name="", endpoint="http://h/a2a")
    monkeypatch.setattr(peers_mod.PeerStore, "find", lambda self, slug: rec)
    assert s._remote_peer_name("vps") == "vps"


def test_remote_peer_name_store_error_falls_back(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod
    from ginno_runtime.api import sessions as s

    def _boom(self, slug):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(peers_mod.PeerStore, "find", _boom)
    assert s._remote_peer_name("vps") == "vps"


def test_with_remote_peer_name_payload_exits(monkeypatch):
    from ginno_runtime.a2a import peers as peers_mod
    from ginno_runtime.api import sessions as s

    # store 钉死：存在 vps → 「VPS 节点」；不存在 ghost → 回 slug
    rec = peers_mod.PeerRecord(slug="vps", name="VPS 节点")
    monkeypatch.setattr(
        peers_mod.PeerStore,
        "find",
        lambda self, slug: rec if slug == "vps" else None,
    )
    # 非远端会话：原样（浅拷贝）、绝不加 remote_peer_name 键
    m = {"id": "s1", "remote_peer": None}
    out = s._with_remote_peer_name(m)
    assert out == {"id": "s1", "remote_peer": None}
    assert "remote_peer_name" not in out
    # 远端会话：补键且不吞其它字段（get_session/create/list 三出口共用）
    m2 = {"id": "s2", "remote_peer": "vps", "title": "t"}
    out2 = s._with_remote_peer_name(m2)
    assert out2["remote_peer_name"] == "VPS 节点"
    assert out2["title"] == "t"
    assert m2 == {"id": "s2", "remote_peer": "vps", "title": "t"}  # 原 dict 不被改写
    m3 = {"id": "s3", "remote_peer": "ghost"}
    assert s._with_remote_peer_name(m3)["remote_peer_name"] == "ghost"
