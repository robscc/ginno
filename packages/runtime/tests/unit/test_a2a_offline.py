"""peer_offline fail-fast + 断网/重连/超时用例（N3，主设计验收 #36）。

被测对象是 tools/a2a_tools.py 的工具层——**绝不 mock 被测对象本身**：
A2AClient 用真实实现（JSON-RPC 组包/错误包装/自愈判定全真跑），只把传输层
换成 httpx.MockTransport 造不可达（连接拒绝/DNS/超时/挂起）。零真网络、
零端口占用。

覆盖：
- 连接拒绝 / DNS 失败 / 各类传输超时 → peer_offline（fail-fast）
- wait_for 外层超时（对端挂起不响应）→ peer_offline
- 离线不排队不重试：传输层调用次数恒 = 1；带 context_id 也不进自愈分支
- 对端恢复后重新派活自然成功（无特殊重连逻辑）
- 错误诊断不含明文 token（SEC-01：••••+尾 4；能力地址 path token 同样掩）
- 协议层错误（JSON-RPC error / HTTP 4xx/5xx）不冒充离线
- task 工具 get/cancel 同语义；自愈重发途中掉线 → peer_offline 且不再重试
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a.client import A2AClient
from ginno_runtime.a2a.contexts import ContextMap
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.tools import a2a_tools

pytestmark = pytest.mark.unit


# ------------------------------ fixtures ---------------------------------- #


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("GINNO_A2A", "1")


@pytest.fixture()
def store(tmp_path):
    return PeerStore(tmp_path / "peers.json", contexts_path=tmp_path / "contexts.json")


@pytest.fixture()
def cm(tmp_path):
    return ContextMap(tmp_path / "contexts.json")


def _peer(slug="expense-bot", **kw):
    defaults = dict(name="报销助手", endpoint="https://bot.example/a2a")
    defaults.update(kw)
    return PeerRecord(slug=slug, **defaults)


class _Wire:
    """MockTransport 的记录器：请求计数 = 真实出站调用次数（重试断言用）。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self._handler = None

    def mount(self, handler):
        self._handler = handler

    async def dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._handler(request)

    @property
    def call_count(self) -> int:
        return len(self.requests)


@pytest.fixture()
def wire(monkeypatch):
    """真实 A2AClient + 注入 MockTransport（只桩传输层，协议逻辑全真）。

    注入的 http_client 不归 A2AClient 所有（aclose 不关它），子类补关，
    避免 unclosed client 告警。
    """
    w = _Wire()

    class TransportClient(A2AClient):
        def __init__(self, endpoint, **kw):
            self._injected = httpx.AsyncClient(transport=httpx.MockTransport(w.dispatch))
            super().__init__(endpoint, http_client=self._injected, **kw)

        async def aclose(self):
            await super().aclose()
            await self._injected.aclose()

    monkeypatch.setattr(a2a_tools, "A2AClient", TransportClient)
    return w


def _build(store, cm, peer=None, **kw):
    if peer is not None:
        store.add(peer)
    return {t.name: t for t in a2a_tools.build_a2a_tools(store=store, contexts=cm, **kw)}


def _rpc_ok(request: httpx.Request, result: dict) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _rpc_err(request: httpx.Request, code: int, message: str, data=None) -> httpx.Response:
    body = json.loads(request.content)
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": err})


def _task_result(tid="t-1", cid="c-1", state="completed", text="报销已完成"):
    return {
        "id": tid, "contextId": cid, "status": {"state": state},
        "artifacts": [{"parts": [{"kind": "text", "text": text}]}],
    }


# --------------------- 不可达三态 → peer_offline --------------------------- #


@pytest.mark.parametrize(
    "exc_factory,reason_frag",
    [
        # 连接拒绝
        (lambda: httpx.ConnectError("[Errno 61] Connection refused"), "ConnectError"),
        # DNS 失败（httpx 把 gaierror 包成 ConnectError——用真 gaierror 构造）
        (
            lambda: httpx.ConnectError(
                str(socket.gaierror(8, "nodename nor servname provided, or not known"))
            ),
            "nodename nor servname",
        ),
        # 建连超时
        (lambda: httpx.ConnectTimeout("connect timed out"), "ConnectTimeout"),
        # 读超时（请求已发出，对端不回包）
        (lambda: httpx.ReadTimeout("read timed out"), "ReadTimeout"),
        # 连接池超时
        (lambda: httpx.PoolTimeout("pool timed out"), "PoolTimeout"),
        # 对端中途掉线
        (lambda: httpx.ReadError("connection reset by peer"), "ReadError"),
    ],
)
async def test_unreachable_peer_offline_fail_fast(store, cm, wire, exc_factory, reason_frag):
    async def handler(request):
        raise exc_factory()

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_delegate"].ainvoke({"task": "写一份报告"})
    assert out.startswith("[error] [a2a:expense-bot]")
    assert "peer_offline" in out
    assert reason_frag in out  # 不可达原因可操作
    assert "不排队不重试" in out
    # fail-fast：传输层只被调用 1 次——不排队、不重试。
    assert wire.call_count == 1


async def test_wait_timeout_peer_offline(store, cm, wire):
    """对端挂起不响应 → wait_for 外层超时（asyncio.TimeoutError 路径）。"""

    async def handler(request):
        await asyncio.sleep(30)  # 远超 task_timeout_s=1，会被 wait_for 取消
        return _rpc_ok(request, _task_result())

    wire.mount(handler)
    tools = _build(store, cm, _peer(task_timeout_s=1))
    out = await tools["a2a_expense_bot_delegate"].ainvoke({"task": "写一份报告"})
    assert out.startswith("[error] [a2a:expense-bot]")
    assert "peer_offline" in out
    assert "超时" in out
    assert wire.call_count == 1


async def test_offline_does_not_trigger_self_heal(store, cm, wire):
    """带 context_id（自愈前置条件）也不得因离线重发——自愈只服务 context 失效。"""

    async def handler(request):
        raise httpx.ConnectError("[Errno 61] Connection refused")

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_delegate"].ainvoke(
        {"task": "追问", "context_id": "c-old"}
    )
    assert "peer_offline" in out
    assert "自愈" not in out  # 未进自愈分支
    assert wire.call_count == 1  # 调用次数 = 1，无重发


# ------------------------- 重连语义：恢复即成功 ---------------------------- #


async def test_peer_recovers_next_delegate_succeeds(store, cm, wire):
    """首次离线 fail-fast；对端恢复后下次调用自然成功（无需特殊重连逻辑）。"""
    n = {"calls": 0}

    async def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return _rpc_ok(request, _task_result())

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    delegate = tools["a2a_expense_bot_delegate"]

    out1 = await delegate.ainvoke({"task": "第一次派活"})
    assert "peer_offline" in out1

    out2 = await delegate.ainvoke({"task": "恢复后再派"})
    assert out2.startswith("[a2a:expense-bot]")
    assert "task=t-1" in out2 and "state=completed" in out2
    assert "peer_offline" not in out2
    # 1 次离线 + 1 次成功，中间无隐藏重试。
    assert wire.call_count == 2


# --------------------------- token 掩码纪律 -------------------------------- #


async def test_offline_message_masks_capability_address_token(store, cm, wire):
    """能力地址 path 即 token：异常文本带完整 URL 也不得泄露明文。"""
    tok = "ABCDEFGH234567ABCDEFGH2345"  # 26 字符 base32 形态
    peer = _peer(endpoint=f"https://node.example:8790/a2a/{tok}", token=tok)

    async def handler(request):
        # 模拟 httpx 异常文本携带完整 URL（部分代理/连接错误会带）
        raise httpx.ConnectError(f"all connection attempts to {peer.endpoint} failed")

    wire.mount(handler)
    tools = _build(store, cm, peer)
    out = await tools["a2a_expense_bot_delegate"].ainvoke({"task": "t"})
    assert "peer_offline" in out
    assert tok not in out  # 明文绝不回流
    assert "••••" + tok[-4:] in out  # SEC-01 掩码口径
    assert "node.example:8790" in out  # host 保留，可操作


async def test_offline_message_masks_bearer_token(store, cm, wire):
    tok = "sk-super-secret-bearer-token-9876543210"
    peer = _peer(token=tok, auth_scheme="bearer")

    async def handler(request):
        raise httpx.ConnectError(f"proxy refused Authorization: Bearer {tok}")

    wire.mount(handler)
    tools = _build(store, cm, peer)
    out = await tools["a2a_expense_bot_delegate"].ainvoke({"task": "t"})
    assert "peer_offline" in out
    assert tok not in out
    assert "••••" + tok[-4:] in out
    # 请求确实带了凭据（证明掩的是真凭据，不是没发）
    assert wire.requests[0].headers["Authorization"] == f"Bearer {tok}"


# --------------------------- task 工具同语义 ------------------------------- #


@pytest.mark.parametrize("action", ["get", "cancel"])
async def test_task_tool_offline_fail_fast(store, cm, wire, action):
    async def handler(request):
        raise httpx.ConnectError("[Errno 61] Connection refused")

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_task"].ainvoke({"task_id": "t-1", "action": action})
    assert out.startswith("[error] [a2a:expense-bot]")
    assert "peer_offline" in out
    assert wire.call_count == 1


async def test_task_tool_wait_timeout_offline(store, cm, wire):
    async def handler(request):
        await asyncio.sleep(30)
        return _rpc_ok(request, _task_result())

    wire.mount(handler)
    tools = _build(store, cm, _peer(task_timeout_s=1))
    out = await tools["a2a_expense_bot_task"].ainvoke({"task_id": "t-1"})
    assert "peer_offline" in out
    assert wire.call_count == 1


# ------------------- 协议层错误不冒充离线（边界守卫） ----------------------- #


@pytest.mark.parametrize("kind", ["rpc_error", "http_500", "http_401"])
async def test_protocol_error_not_offline(store, cm, wire, kind):
    async def handler(request):
        if kind == "rpc_error":
            return _rpc_err(request, -32603, "internal boom")
        if kind == "http_500":
            return httpx.Response(500, text="server on fire")
        return httpx.Response(401, text="unauthorized")

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_delegate"].ainvoke({"task": "t"})
    assert out.startswith("[error]")
    assert "peer_offline" not in out  # 对端在线，只是拒绝/出错
    assert wire.call_count == 1


# --------------------- 自愈与离线的交互（wire 级实证） ---------------------- #


async def test_self_heal_still_retries_once_when_online(store, cm, wire):
    """守卫既有 §9.6 语义：context 失效（协议错误）仍自愈重发 1 次。"""
    n = {"calls": 0}

    async def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            return _rpc_err(
                request, -32001, "context expired",
                data={"ginno:retention": {"reason": "context_expired"}},
            )
        return _rpc_ok(request, _task_result(cid="c-new"))

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_delegate"].ainvoke(
        {"task": "t", "context_id": "c-old"}
    )
    assert "state=completed" in out
    assert wire.call_count == 2  # 原始 1 次 + 自愈 1 次


async def test_offline_during_self_heal_stops(store, cm, wire):
    """自愈重发途中对端掉线 → peer_offline，且不再继续重试（共 2 次封顶）。"""
    n = {"calls": 0}

    async def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            return _rpc_err(
                request, -32001, "context expired",
                data={"ginno:retention": {"reason": "context_expired"}},
            )
        raise httpx.ConnectError("[Errno 61] Connection refused")

    wire.mount(handler)
    tools = _build(store, cm, _peer())
    out = await tools["a2a_expense_bot_delegate"].ainvoke(
        {"task": "t", "context_id": "c-old"}
    )
    assert "peer_offline" in out
    assert wire.call_count == 2  # 原始 + 自愈各 1 次，离线后不再重试
