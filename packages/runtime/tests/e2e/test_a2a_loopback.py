"""A2A 回环 e2e —— FakeA2APeer 假对端 + 真 client/peers/contexts/tools 全链路。

设计 §11.3.1 吸收清单的 ``GINNO_FAKE_A2A_PEER`` 测试缝（照 GINNO_FAKE_LLM
先例）：**只有对端是假的**——A2AClient、PeerStore、ContextMap、
build_a2a_tools 全部走真实现，fake peer 真起在 127.0.0.1 随机端口（不碰
8787，无真网络外联，CI 干净环境可跑，不依赖 dist sidecar）。

覆盖设计 §13.1 验收条目：
- #1  复制地址→粘贴→连接成功（parse_address → fetch_card → PeerStore → 首次委托）
- #15 同会话追问：复用 contextId、新 taskId
- #21 远端 context 失效自愈（task_purged 复用 context / context_expired 开新 context）
- #22 取消已 purged 的 task 视为成功（CancelTask 幂等）
- #39 零真机全链路（本文件整体即其 P0 子集：委托→中断→续跑→产物）

剧本 schema 见 ginno_runtime/a2a/fake.py 模块 docstring。
"""

from __future__ import annotations

import re

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a.address import parse_address
from ginno_runtime.a2a.card import fetch_card, validate_card
from ginno_runtime.a2a.contexts import ContextMap
from ginno_runtime.a2a.fake import (
    fake_peer,
    fake_peer_enabled,
    retention_error,
    start_fake_peer,
    stop_fake_peers,
)
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.tools.a2a_tools import build_a2a_tools

pytestmark = pytest.mark.e2e

_SID = "sid-loopback"
_SLUG = "fake-peer"
_KEY = f"{_SID}:{_SLUG}"
_HEADER = re.compile(r"task=(\S+) state=(\S+) context=(\S+)")


@pytest.fixture(autouse=True)
def _a2a_on(monkeypatch):
    """出站工具 opt-in 门控（world_state.a2a_enabled 的 env 覆盖）。"""
    monkeypatch.setenv("GINNO_A2A", "1")


@pytest.fixture()
def stores(tmp_path):
    store = PeerStore(tmp_path / "peers.json", contexts_path=tmp_path / "contexts.json")
    cm = ContextMap(tmp_path / "contexts.json")
    return store, cm


def _tools(store, cm, endpoint, *, token="", auth_scheme="none"):
    """真 PeerStore 加 peer → 真 build_a2a_tools → (delegate, task) 工具对。"""
    store.add(PeerRecord(
        slug=_SLUG, name="Fake Peer", endpoint=endpoint, token=token,
        auth_scheme=auth_scheme, confirm_mode="always", task_timeout_s=10,
    ))
    tools = build_a2a_tools(session_id=_SID, project_slug="loopback",
                            store=store, contexts=cm)
    by_name = {t.name: t for t in tools}
    return by_name["a2a_fake_peer_delegate"], by_name["a2a_fake_peer_task"]


def _header(text: str) -> tuple[str, str, str]:
    """从工具结果头解析 (taskId, state, contextId)。"""
    assert "[error]" not in text, text
    m = _HEADER.search(text)
    assert m, f"no task header in: {text}"
    return m.group(1), m.group(2), m.group(3)


# --------------------------------------------------------------------------- #
# #1 复制地址 → 粘贴 → 连接成功
# --------------------------------------------------------------------------- #

async def test_paste_capability_address_connects(stores):
    store, cm = stores
    async with fake_peer({"send": [{"result": "connected ok"}]}) as peer:
        # 「复制地址」：fake peer 签发能力地址；「粘贴」：真 parse_address 解析
        address = peer.address
        parsed = parse_address(address)
        assert parsed.is_capability and parsed.token == peer.token
        # 连接第 1 步：Card 发现（127.0.0.1 属显式配置白名单，allow_private）
        card = await fetch_card(parsed.endpoint, allow_private=True)
        assert validate_card(card) == []
        assert card.url == parsed.endpoint
        # 连接第 2 步：注册 peer（真 PeerStore）→ 首次委托即成功，无额外确认
        delegate, _task = _tools(store, cm, parsed.endpoint)
        assert store.find(_SLUG).endpoint == parsed.endpoint
        out = await delegate.ainvoke({"task": "hello remote"})
        assert "connected ok" in out
        tid, state, cid = _header(out)
        assert state == "completed" and tid and cid


# --------------------------------------------------------------------------- #
# 委托 → taskId + 结果文本 + ContextMap 簿记
# --------------------------------------------------------------------------- #

async def test_delegate_returns_task_and_books_context(stores):
    store, cm = stores
    async with fake_peer({"send": [{"states": ["completed"], "result": "done text"}]}) as peer:
        delegate, _task = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out = await delegate.ainvoke({"task": "do the thing"})
        assert out.startswith(f"[a2a:{_SLUG}]")
        tid, state, cid = _header(out)
        assert state == "completed"
        assert "done text" in out
        rec = cm.get_or_none(_KEY)
        assert rec is not None
        assert rec.context_id == cid
        assert rec.tasks[0].task_id == tid
        assert rec.tasks[0].state == "completed"


# --------------------------------------------------------------------------- #
# #15 同会话追问：复用 contextId，新 taskId
# --------------------------------------------------------------------------- #

async def test_followup_reuses_context_with_new_task(stores):
    store, cm = stores
    scripts = {"send": [{"result": "first answer"}, {"result": "second answer"}]}
    async with fake_peer(scripts) as peer:
        delegate, _task = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out1 = await delegate.ainvoke({"task": "first question"})
        tid1, _s1, cid1 = _header(out1)
        out2 = await delegate.ainvoke({"task": "follow-up", "context_id": cid1})
        tid2, _s2, cid2 = _header(out2)
        assert cid2 == cid1  # 复用 contextId
        assert tid2 != tid1  # 新 taskId（终态任务不可重启，§8.1）
        assert "second answer" in out2
        # 追问请求确实带着 contextId 出网（fake 记录的是真客户端发的报文）
        followup = peer.requests[1]["params"]["message"]
        assert followup["contextId"] == cid1
        rec = cm.get_or_none(_KEY)
        assert {t.task_id for t in rec.tasks} == {tid1, tid2}


# --------------------------------------------------------------------------- #
# 状态机：working → completed（get 推进 + 影子状态刷新）
# --------------------------------------------------------------------------- #

async def test_get_advances_working_to_completed(stores):
    store, cm = stores
    scripts = {"send": [{"states": ["working", "completed"], "result": "final text"}]}
    async with fake_peer(scripts) as peer:
        delegate, task_tool = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out = await delegate.ainvoke({"task": "slow job"})
        tid, state, _cid = _header(out)
        assert state == "working"
        assert "final text" not in out  # working 中还没有产物
        got = await task_tool.ainvoke({"task_id": tid, "action": "get"})
        tid2, state2, _ = _header(got)
        assert tid2 == tid and state2 == "completed"
        assert "final text" in got
        assert cm.get_or_none(_KEY).tasks[0].state == "completed"


# --------------------------------------------------------------------------- #
# #22 CancelTask 幂等：purged 后 -32001 视为取消成功
# --------------------------------------------------------------------------- #

async def test_cancel_idempotent_after_purge(stores):
    store, cm = stores
    scripts = {
        "send": [{"result": "r"}],
        # 第一次 cancel 正常；第二次对端已 purge → -32001 + task_purged
        "cancel": [{}, {"error": retention_error("task_purged")}],
    }
    async with fake_peer(scripts) as peer:
        delegate, task_tool = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out = await delegate.ainvoke({"task": "job"})
        tid, _state, _cid = _header(out)
        first = await task_tool.ainvoke({"task_id": tid, "action": "cancel"})
        assert "[error]" not in first
        assert "state=canceled" in first and "已发送取消" in first
        # purged 后再取消：客户端必须视为成功（§9.6），不是 [error]
        again = await task_tool.ainvoke({"task_id": tid, "action": "cancel"})
        assert "[error]" not in again
        assert "state=canceled" in again and "视为取消成功" in again
        assert cm.get_or_none(_KEY).tasks[0].state == "canceled"


# --------------------------------------------------------------------------- #
# #21 自愈：task_purged → 复用 context 开新 task
# --------------------------------------------------------------------------- #

async def test_self_heal_task_purged_reuses_context(stores):
    store, cm = stores
    scripts = {"send": [
        {"result": "answer 1"},
        {"error": retention_error("task_purged")},  # 带旧 context 委托 → 命中
        {"result": "answer 2"},                     # 自愈重发（同 context）→ 成功
    ]}
    async with fake_peer(scripts) as peer:
        delegate, _task = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out1 = await delegate.ainvoke({"task": "q1"})
        tid1, _s1, cid1 = _header(out1)
        out2 = await delegate.ainvoke({"task": "q2", "context_id": cid1})
        assert "[error]" not in out2
        assert "远端上下文已重置" not in out2  # task_purged 不重置 context
        tid2, _s2, cid2 = _header(out2)
        assert cid2 == cid1  # context 存活 → 复用
        assert tid2 != tid1  # 开新 task
        assert "answer 2" in out2
        # 真客户端共发 3 次 send：原始 + 错误 + 自愈重发（最多 1 次）
        assert len(peer.requests) == 3
        assert peer.requests[2]["params"]["message"]["contextId"] == cid1
        assert cm.get_or_none(_KEY).context_id == cid1


# --------------------------------------------------------------------------- #
# #21 自愈：context_expired → 省略 contextId 开新 context + 提示
# --------------------------------------------------------------------------- #

async def test_self_heal_context_expired_opens_new_context(stores):
    store, cm = stores
    scripts = {"send": [
        {"result": "answer 1"},
        {"error": retention_error("context_expired")},
        {"result": "answer 2"},
    ]}
    async with fake_peer(scripts) as peer:
        delegate, _task = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out1 = await delegate.ainvoke({"task": "q1"})
        tid1, _s1, cid1 = _header(out1)
        out2 = await delegate.ainvoke({"task": "q2", "context_id": cid1})
        assert "[error]" not in out2
        assert "远端上下文已重置" in out2  # 提示用户 context 已重置
        tid2, _s2, cid2 = _header(out2)
        assert cid2 != cid1  # 开新 context
        assert tid2 != tid1
        # 自愈重发不带旧 contextId（绝不带旧值反复重试）
        assert "contextId" not in peer.requests[2]["params"]["message"]
        rec = cm.get_or_none(_KEY)
        assert rec.context_id == cid2
        # 旧 context 的幽灵 task 已清（record_peer_reset），只留新 task
        assert [t.task_id for t in rec.tasks] == [tid2]


# --------------------------------------------------------------------------- #
# #39 零真机全链路：委托 → 中断（input-required）→ 续跑 → 产物
# --------------------------------------------------------------------------- #

async def test_input_required_interrupt_then_resume(stores):
    store, cm = stores
    scripts = {"send": [
        {"states": ["input-required"], "status_message": "请补充预算上限",
         "result": "final report"},
        {"states": ["completed"], "result": "final report"},
    ]}
    async with fake_peer(scripts) as peer:
        delegate, _task = _tools(store, cm, f"{peer.base_url}/a2a/{peer.token}")
        out1 = await delegate.ainvoke({"task": "make a plan"})
        tid1, state1, cid1 = _header(out1)
        assert state1 == "input-required"
        assert "请补充预算上限" in out1  # status.message 回流
        assert "等待补充输入" in out1    # 工具层中断提示
        assert "final report" not in out1  # 中断态没有产物
        # 同 context 续跑 → 新 task 到终态拿产物
        out2 = await delegate.ainvoke({"task": "预算 1 万", "context_id": cid1})
        tid2, state2, cid2 = _header(out2)
        assert state2 == "completed" and cid2 == cid1 and tid2 != tid1
        assert "final report" in out2


# --------------------------------------------------------------------------- #
# bearer 鉴权形态 + 错 token 401（#2 的出站侧投影）
# --------------------------------------------------------------------------- #

async def test_bearer_auth_roundtrip_and_wrong_token(stores):
    store, cm = stores
    async with fake_peer({"send": [{"result": "bearer ok"}, {"result": "unused"}]}) as peer:
        delegate, _task = _tools(
            store, cm, peer.base_url + "/", token=peer.token, auth_scheme="bearer",
        )
        out = await delegate.ainvoke({"task": "hi"})
        assert "bearer ok" in out
        # token 改错 → 401 → 工具压平成 [error]（绝不 raise）。
        # 工具构建时钉死 peer 快照（seam 原则 2），改 store 后须重建工具。
        store.update(_SLUG, {"token": "WRONG-TOKEN-WRONG-TOKEN-WRONG"})
        rebuilt = build_a2a_tools(session_id=_SID, project_slug="loopback",
                                  store=store, contexts=cm)
        bad_delegate = {t.name: t for t in rebuilt}["a2a_fake_peer_delegate"]
        bad = await bad_delegate.ainvoke({"task": "hi again"})
        assert bad.startswith("[error]")
        assert "401" in bad


# --------------------------------------------------------------------------- #
# 环境缝：GINNO_FAKE_A2A_PEER=1 + GINNO_FAKE_A2A_SCRIPTS 文件
# --------------------------------------------------------------------------- #

async def test_env_seam_start_fake_peer(stores, tmp_path, monkeypatch):
    store, cm = stores
    scripts_file = tmp_path / "a2a-scripts.json"
    scripts_file.write_text(json.dumps({"send": [{"result": "from env scripts"}]}),
                            encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_A2A_PEER", "1")
    monkeypatch.setenv("GINNO_FAKE_A2A_SCRIPTS", str(scripts_file))
    assert fake_peer_enabled() is True
    base_url, token = await start_fake_peer()  # scripts=None → 读环境缝
    try:
        assert base_url.startswith("http://127.0.0.1:")
        delegate, _task = _tools(store, cm, f"{base_url}/a2a/{token}")
        out = await delegate.ainvoke({"task": "env seam"})
        assert "from env scripts" in out
    finally:
        await stop_fake_peers()
