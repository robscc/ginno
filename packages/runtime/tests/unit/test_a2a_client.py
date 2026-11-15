"""A2A JSON-RPC 客户端单测（httpx MockTransport 全链路，零真网络）。"""

from __future__ import annotations

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a import card
from ginno_runtime.a2a.address import fingerprint_of
from ginno_runtime.a2a.client import A2AClient
from ginno_runtime.a2a.errors import (
    A2AError,
    TaskNotCancelableError,
    TaskNotFoundError,
    context_survives,
    is_not_found,
    retention_reason,
)
from ginno_runtime.a2a.models import Message, Task, TaskState

pytestmark = pytest.mark.unit

ENDPOINT = "https://peer.example.com/a2a"
_DER = b"\x30\x82\x01\x0a-fake-peer-cert"

_TASK_RESULT = {
    "id": "t-1",
    "contextId": "c-1",
    "status": {"state": "working", "timestamp": "2026-11-15T00:00:00Z"},
    "artifacts": [
        {"artifactId": "a-1", "name": "report", "parts": [{"kind": "text", "text": "done"}]}
    ],
}


class _RpcRecorder:
    """按 method 分发 JSON-RPC 响应；自动回显请求 id（除非响应自带）。"""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict] = []
        self.responses: dict = {}  # method -> 完整响应 dict 或 callable(body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        self.bodies.append(body)
        resp = self.responses.get(body["method"])
        if callable(resp):
            resp = resp(body)
        if resp is None:
            resp = {"jsonrpc": "2.0", "result": {}}
        if isinstance(resp, dict) and "id" not in resp:
            resp = {**resp, "id": body["id"]}
        return httpx.Response(200, json=resp)


def _client(rec: _RpcRecorder, **kw) -> A2AClient:
    return A2AClient(
        ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)), **kw
    )


def _rpc_error(code: int, message: str = "err", data=None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "error": err}


# ------------------------------ send_message ------------------------------ #
async def test_send_message_returns_message():
    rec = _RpcRecorder()
    rec.responses["message/send"] = {
        "jsonrpc": "2.0",
        "result": {
            "role": "agent",
            "parts": [{"kind": "text", "text": "已受理"}],
            "messageId": "m-1",
        },
    }
    out = await _client(rec).send_message(["帮我把这张发票报销了"])
    assert isinstance(out, Message)
    assert out.text() == "已受理"
    assert out.role == "agent"


async def test_send_message_returns_task_when_status_present():
    rec = _RpcRecorder()
    rec.responses["message/send"] = {"jsonrpc": "2.0", "result": _TASK_RESULT}
    out = await _client(rec).send_message(["hi"])
    assert isinstance(out, Task)
    assert out.id == "t-1"
    assert out.context_id == "c-1"
    assert out.state is TaskState.WORKING
    assert out.result_text() == "done"


async def test_send_message_payload_shape():
    rec = _RpcRecorder()
    await _client(rec).send_message(
        ["第一段", "第二段"], context_id="c-9", reference_task_ids=["t-old"]
    )
    body = rec.bodies[0]
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "message/send"
    msg = body["params"]["message"]
    assert msg["role"] == "user"
    assert msg["parts"] == [
        {"kind": "text", "text": "第一段"},
        {"kind": "text", "text": "第二段"},
    ]
    assert msg["messageId"]  # 客户端生成，非空
    assert msg["contextId"] == "c-9"
    assert msg["referenceTaskIds"] == ["t-old"]
    assert msg["kind"] == "message"


async def test_send_message_omits_optional_ids_when_absent():
    rec = _RpcRecorder()
    await _client(rec).send_message("hi")  # 单字符串也接受
    msg = rec.bodies[0]["params"]["message"]
    assert "contextId" not in msg
    assert "referenceTaskIds" not in msg
    assert msg["parts"] == [{"kind": "text", "text": "hi"}]


async def test_send_message_posts_to_endpoint_with_json_content_type():
    rec = _RpcRecorder()
    await _client(rec).send_message("hi")
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == ENDPOINT
    assert req.headers["Content-Type"] == "application/json"


# ------------------------------ get / cancel ------------------------------ #
async def test_get_task():
    rec = _RpcRecorder()
    rec.responses["tasks/get"] = {"jsonrpc": "2.0", "result": _TASK_RESULT}
    task = await _client(rec).get_task("t-1")
    assert isinstance(task, Task)
    assert task.id == "t-1"
    assert rec.bodies[0]["method"] == "tasks/get"
    assert rec.bodies[0]["params"] == {"id": "t-1"}


async def test_cancel_task_returns_raw_dict():
    rec = _RpcRecorder()
    rec.responses["tasks/cancel"] = {
        "jsonrpc": "2.0",
        "result": {"id": "t-1", "status": {"state": "canceled"}},
    }
    out = await _client(rec).cancel_task("t-1")
    assert isinstance(out, dict)
    assert out["status"]["state"] == "canceled"
    assert rec.bodies[0]["method"] == "tasks/cancel"
    assert rec.bodies[0]["params"] == {"id": "t-1"}


# ------------------------------ 错误映射 ------------------------------ #
async def test_error_minus_32001_maps_to_task_not_found():
    rec = _RpcRecorder()
    rec.responses["tasks/get"] = _rpc_error(
        -32001, "gone", data={"ginno:retention": {"reason": "task_purged"}}
    )
    with pytest.raises(TaskNotFoundError) as ei:
        await _client(rec).get_task("t-x")
    err = ei.value
    assert err.code == -32001
    # 自愈语义链路：task_purged → context 存活 → 复用 contextId 开新 task（设计 §9.6）
    assert retention_reason(err) == "task_purged"
    assert context_survives(err) is True


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [
        (-32002, TaskNotCancelableError),
        (-32008, A2AError),  # ExtensionSupportRequiredError 是 A2AError 子类
        (-32009, A2AError),
        (-32601, A2AError),  # JSON-RPC 标准码：方法不存在
    ],
)
async def test_error_code_mapping(code, exc_type):
    rec = _RpcRecorder()
    rec.responses["message/send"] = _rpc_error(code, "boom")
    with pytest.raises(exc_type) as ei:
        await _client(rec).send_message("hi")
    assert ei.value.code == code


async def test_cancel_not_found_is_success_semantics():
    # 验收 #22：purged 后 cancel 回 TaskNotFoundError，调用方视为成功
    rec = _RpcRecorder()
    rec.responses["tasks/cancel"] = _rpc_error(-32001, "purged")
    with pytest.raises(TaskNotFoundError) as ei:
        await _client(rec).cancel_task("t-gone")
    assert is_not_found(ei.value) is True


async def test_rpc_error_in_non_200_response_still_mapped():
    # gRPC/HTTP 绑定映射：404 + JSON-RPC error body，语义不能丢
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=_rpc_error(-32001, "not found"))

    c = A2AClient(ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(TaskNotFoundError):
        await c.get_task("t-x")


# ------------------------------ id / 头 / 传输 ------------------------------ #
async def test_request_id_monotonic():
    rec = _RpcRecorder()
    c = _client(rec)
    await c.get_task("t-1")
    await c.get_task("t-2")
    await c.cancel_task("t-1")
    assert [b["id"] for b in rec.bodies] == [1, 2, 3]


async def test_auth_headers_bearer_and_api_key():
    rec = _RpcRecorder()
    await _client(rec, token="tk-1", api_key="ak-1").send_message("hi")
    headers = rec.requests[0].headers
    assert headers["Authorization"] == "Bearer tk-1"
    assert headers["X-Api-Key"] == "ak-1"


async def test_no_auth_headers_when_no_credentials():
    rec = _RpcRecorder()
    await _client(rec).send_message("hi")
    headers = rec.requests[0].headers
    assert "Authorization" not in headers
    assert "X-Api-Key" not in headers


@pytest.mark.parametrize("status", [500, 401, 403])
async def test_http_error_without_rpc_body_wrapped(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream exploded")

    c = A2AClient(ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(A2AError, match=str(status)):
        await c.get_task("t-1")


async def test_transport_error_wrapped_as_a2a_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = A2AClient(ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    with pytest.raises(A2AError, match="网络错误"):
        await c.get_task("t-1")


async def test_response_id_mismatch_rejected():
    rec = _RpcRecorder()
    rec.responses["tasks/get"] = {"jsonrpc": "2.0", "id": 999, "result": _TASK_RESULT}
    with pytest.raises(A2AError, match="id"):
        await _client(rec).get_task("t-1")


async def test_missing_result_rejected():
    rec = _RpcRecorder()
    rec.responses["tasks/get"] = {"jsonrpc": "2.0"}
    with pytest.raises(A2AError, match="result"):
        await _client(rec).get_task("t-1")


async def test_non_json_response_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    c = A2AClient(ENDPOINT, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(A2AError):
        await c.get_task("t-1")


# ------------------------------ 指纹 pinning ------------------------------ #
async def test_verify_fp_checked_once_before_first_rpc(monkeypatch):
    calls = []

    def fake_peer(host, port, timeout_s):
        calls.append((host, port))
        return _DER

    monkeypatch.setattr(card, "_peer_cert_der", fake_peer)
    rec = _RpcRecorder()
    c = _client(rec, verify_fp=fingerprint_of(_DER))
    await c.get_task("t-1")
    await c.get_task("t-2")
    assert calls == [("peer.example.com", 443)]  # 只校验一次，后续复用结论
    assert len(rec.requests) == 2


async def test_verify_fp_mismatch_blocks_before_any_rpc(monkeypatch):
    monkeypatch.setattr(card, "_peer_cert_der", lambda h, p, t: _DER)
    rec = _RpcRecorder()
    c = _client(rec, verify_fp=fingerprint_of(b"impostor"))
    with pytest.raises(A2AError, match="指纹不匹配"):
        await c.send_message("hi")
    assert rec.requests == []  # 校验失败 → 凭据一个字节都不发


async def test_verify_fp_default_port_http(monkeypatch):
    calls = []

    def fake_peer(host, port, timeout_s):
        calls.append((host, port))
        return _DER

    monkeypatch.setattr(card, "_peer_cert_der", fake_peer)
    rec = _RpcRecorder()
    c = A2AClient(
        "http://peer.example.com/a2a",
        verify_fp=fingerprint_of(_DER),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)),
    )
    await c.get_task("t-1")
    assert calls == [("peer.example.com", 80)]


# ------------------------------ 构造 / 生命周期 ------------------------------ #
def test_bare_domain_endpoint_gets_https():
    c = A2AClient("peer.example.com/a2a")
    assert c.endpoint == "https://peer.example.com/a2a"


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "ftp://peer.example.com/a2a", ""])
def test_bad_endpoint_rejected(bad):
    with pytest.raises(ValueError):
        A2AClient(bad)


async def test_aclose_does_not_close_injected_client():
    rec = _RpcRecorder()
    injected = httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))
    c = A2AClient(ENDPOINT, http_client=injected)
    await c.aclose()
    assert not injected.is_closed  # 注入的 client 生命周期归注入方
    await injected.aclose()


async def test_aclose_closes_owned_client():
    c = A2AClient(ENDPOINT)
    await c.aclose()
    assert c._http.is_closed
