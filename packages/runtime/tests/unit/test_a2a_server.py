"""Unit tests for a2a.server —— 独立入站 FastAPI app（设计 §2.2/§10/§11.2）。

httpx.AsyncClient(transport=ASGITransport(app)) 全链路：
- 401 无 token / WWW-Authenticate 头 / 响应体不泄露存在性差异 / 3s 内踢掉；
- 公开 Card 分级披露（skills 只回 id+name，无 description/examples）；
- message/send → submitted Task；tasks/get；tasks/cancel 幂等 + NotFound；
- 令牌桶限流 429、并发上限 2；审计脱敏；
- **R3 路径探测表**：settings/mcp/skills/agents/providers 全部 404——
  根本不注册，而非注册后拒绝（§11.2 验收期望 404 而非 403）。
"""

from __future__ import annotations

import time

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a import auth, server
from ginno_runtime.a2a.models import AgentSkill, TaskState

pytestmark = pytest.mark.unit

SECRET_DESC = "SECRET-DESCRIPTION-do-not-leak"
SECRET_EXAMPLE = "SECRET-EXAMPLE-do-not-leak"


@pytest.fixture()
def deps(tmp_path):
    return server.A2ADeps(
        tokens=auth.TokenStore(tmp_path / "tokens.json"),
        audit=server.AuditLog(tmp_path / "audit.jsonl"),
        limiter=server.TokenBucketLimiter(30),
        agent_name="Ginno Test",
        agent_version="9.9.9",
        skills=[
            AgentSkill(
                id="skill-1",
                name="Skill One",
                description=SECRET_DESC,
                examples=[SECRET_EXAMPLE],
                tags=["secret-tag"],
            )
        ],
    )


@pytest.fixture()
def token(deps):
    return deps.tokens.issue()


@pytest.fixture()
def app(deps):
    return server.build_a2a_app(deps)


@pytest.fixture()
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://a2a.test"
    ) as c:
        yield c


def rpc(method, params=None, req_id=1):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


def send_payload(text="hello"):
    return rpc(
        "message/send",
        {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}},
    )


async def _send(client, token, text="hello"):
    resp = await client.post(f"/a2a/{token}/", json=send_payload(text))
    assert resp.status_code == 200
    return resp.json()["result"]


# -- 鉴权 -------------------------------------------------------------------


async def test_missing_token_401_with_www_authenticate(client):
    for resp in (
        await client.get("/.well-known/agent-card.json"),
        await client.post("/a2a/", json=rpc("message/send")),
        await client.post("/", json=rpc("message/send")),
    ):
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"


async def test_wrong_token_401_uniform_body_no_existence_oracle(client, token):
    wrong = "A" * 26
    responses = [
        await client.get("/a2a/"),  # 缺 token
        await client.get(f"/a2a/{wrong}/.well-known/agent-card.json"),  # 错 token + 存在的路由
        await client.get(f"/a2a/{wrong}/api/settings"),  # 错 token + R3 路径
        await client.post(f"/a2a/{wrong}/", json=rpc("message/send")),
    ]
    for resp in responses:
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"
    # 任何失败形态响应体逐字节相同——不泄露路径/令牌存在性。
    assert len({resp.content for resp in responses}) == 1


async def test_auth_reject_well_within_3s_deadline(client):
    start = time.monotonic()
    resp = await client.get("/a2a/" + "B" * 26 + "/")
    elapsed = time.monotonic() - start
    assert resp.status_code == 401
    assert elapsed < server.AUTH_REJECT_DEADLINE_S


async def test_revoked_token_401(client, token, deps):
    assert deps.tokens.revoke(token) is True
    resp = await client.get(f"/a2a/{token}/.well-known/agent-card.json")
    assert resp.status_code == 401


# -- 公开 Card 分级披露 -------------------------------------------------------


async def test_valid_token_card_200(client, token):
    resp = await client.get(f"/a2a/{token}/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["name"] == "Ginno Test"
    assert card["version"] == "9.9.9"
    assert "capabilities" in card
    assert card["security"] == [{"bearer": []}]
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"


async def test_card_tiered_disclosure_no_skill_descriptions(client, token):
    resp = await client.get(f"/a2a/{token}/.well-known/agent-card.json")
    card = resp.json()
    assert card["skills"] == [{"id": "skill-1", "name": "Skill One"}]
    for skill in card["skills"]:
        assert set(skill) == {"id", "name"}
    assert SECRET_DESC not in resp.text
    assert SECRET_EXAMPLE not in resp.text
    assert "secret-tag" not in resp.text


# -- JSON-RPC 任务生命周期 ----------------------------------------------------


async def test_message_send_returns_submitted_task(client, token):
    resp = await client.post(f"/a2a/{token}/", json=send_payload("do something"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    task = body["result"]
    assert task["status"]["state"] == "submitted"
    assert task["id"] and task["contextId"]


async def test_tasks_get_roundtrip(client, token):
    task = await _send(client, token)
    resp = await client.post(
        f"/a2a/{token}/tasks/get", json=rpc("tasks/get", {"id": task["id"]})
    )
    result = resp.json()["result"]
    assert result["id"] == task["id"]
    assert result["contextId"] == task["contextId"]
    assert result["status"]["state"] == "submitted"


async def test_tasks_get_not_found_jsonrpc_error(client, token):
    resp = await client.post(
        f"/a2a/{token}/tasks/get", json=rpc("tasks/get", {"id": "nope"}, req_id=7)
    )
    body = resp.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32001
    assert "result" not in body


async def test_tasks_cancel_idempotent(client, token):
    task = await _send(client, token)
    payload = rpc("tasks/cancel", {"id": task["id"]})
    r1 = await client.post(f"/a2a/{token}/tasks/cancel", json=payload)
    assert r1.json()["result"]["status"]["state"] == "canceled"
    r2 = await client.post(f"/a2a/{token}/tasks/cancel", json=payload)
    assert "error" not in r2.json()
    assert r2.json()["result"]["status"]["state"] == "canceled"


async def test_tasks_cancel_not_found_jsonrpc_error(client, token):
    resp = await client.post(
        f"/a2a/{token}/tasks/cancel", json=rpc("tasks/cancel", {"id": "ghost"})
    )
    assert resp.json()["error"]["code"] == -32001


async def test_cancel_terminal_task_not_cancelable(client, token, deps):
    task = await _send(client, token)
    deps.tasks.get(task["id"]).status.state = TaskState.COMPLETED
    resp = await client.post(
        f"/a2a/{token}/tasks/cancel", json=rpc("tasks/cancel", {"id": task["id"]})
    )
    assert resp.json()["error"]["code"] == -32002


async def test_root_endpoint_no_slash_dispatches_all_methods(client, token):
    # 本仓 a2a.client POST 到无尾斜杠能力端点且 httpx 不跟随 307——
    # 主端点必须原样接住全部三个方法。
    resp = await client.post(f"/a2a/{token}", json=send_payload())
    task = resp.json()["result"]
    got = await client.post(f"/a2a/{token}", json=rpc("tasks/get", {"id": task["id"]}))
    assert got.json()["result"]["id"] == task["id"]
    canceled = await client.post(
        f"/a2a/{token}", json=rpc("tasks/cancel", {"id": task["id"]})
    )
    assert canceled.json()["result"]["status"]["state"] == "canceled"


async def test_dedicated_route_rejects_foreign_method(client, token):
    resp = await client.post(f"/a2a/{token}/tasks/get", json=send_payload())
    assert resp.json()["error"]["code"] == -32601


async def test_unknown_rpc_method_not_found(client, token):
    resp = await client.post(f"/a2a/{token}/", json=rpc("settings/update", {}))
    assert resp.json()["error"]["code"] == -32601


async def test_malformed_json_parse_error(client, token):
    resp = await client.post(
        f"/a2a/{token}/",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert resp.json()["error"]["code"] == -32700


# -- 配额 -------------------------------------------------------------------


async def test_rate_limit_429_after_bucket_empty(tmp_path):
    deps = server.A2ADeps(
        tokens=auth.TokenStore(tmp_path / "t.json"),
        audit=server.AuditLog(tmp_path / "a.jsonl"),
        limiter=server.TokenBucketLimiter(2),  # 容量 2：两发后桶空
    )
    token = deps.tokens.issue()
    app = server.build_a2a_app(deps)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        url = f"/a2a/{token}/.well-known/agent-card.json"
        assert (await c.get(url)).status_code == 200
        assert (await c.get(url)).status_code == 200
        limited = await c.get(url)
        assert limited.status_code == 429
        assert "retry-after" in limited.headers


def test_token_bucket_refill_deterministic():
    now = [0.0]
    lim = server.TokenBucketLimiter(60, capacity=2, clock=lambda: now[0])
    assert lim.allow() and lim.allow()
    assert not lim.allow()
    now[0] = 1.0  # 60/min = 1/s → 补一个
    assert lim.allow()
    assert not lim.allow()


async def test_concurrency_cap_2(client, token):
    t1 = await _send(client, token)
    await _send(client, token)
    third = await client.post(f"/a2a/{token}/", json=send_payload())
    assert third.status_code == 429
    assert third.json()["error"] == "too_many_concurrent_tasks"
    # 取消一个腾出名额后可再提交
    await client.post(f"/a2a/{token}/tasks/cancel", json=rpc("tasks/cancel", {"id": t1["id"]}))
    assert (await client.post(f"/a2a/{token}/", json=send_payload())).status_code == 200


# -- R3 路径根本不注册（§11.2：验收期望 404 而非 403）--------------------------

R3_PATHS = [
    "/api/settings",
    "/api/mcp",
    "/api/skills",
    "/api/agents",
    "/api/providers",
]


@pytest.mark.parametrize("path", R3_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_r3_paths_never_registered_404(client, token, path, method):
    resp = await client.request(
        method, f"/a2a/{token}{path}", json={} if method == "POST" else None
    )
    assert resp.status_code == 404


def test_route_table_is_exactly_the_whitelist(app):
    paths = {route.path for route in app.routes}
    assert paths == {
        "/a2a/{token}/.well-known/agent-card.json",
        "/a2a/{token}",
        "/a2a/{token}/",
        "/a2a/{token}/tasks/get",
        "/a2a/{token}/tasks/cancel",
        # P1-3（2026-11-16）：A2A 标准 SSE 进度流两个操作。
        # 仍属协议白名单，不是 R3 类路径（settings/mcp/… 依旧 404）。
        "/a2a/{token}/message/stream",
        "/a2a/{token}/tasks/resubscribe",
    }


# -- 审计脱敏与安全头 ---------------------------------------------------------


async def test_audit_sanitized_no_plaintext_no_content(client, token, deps):
    secret_text = "peer-secret-content-123"
    await client.post(f"/a2a/{token}/", json=send_payload(secret_text))
    await client.get("/a2a/" + "C" * 26 + "/")  # 一条 auth_failed
    lines = deps.audit.path.read_text(encoding="utf-8").splitlines()
    assert lines
    raw = "\n".join(lines)
    assert token not in raw  # 明文 token 绝不落审计
    assert secret_text not in raw  # peer 内容绝不落审计
    entries = [json.loads(line) for line in lines]
    for entry in entries:
        assert set(entry) <= {"ts", "method", "token", "outcome", "task_id"}
        assert entry["token"] == "" or len(entry["token"]) == 8
    sent = [e for e in entries if e["method"] == "message/send"]
    assert sent and sent[0]["token"] == auth.token_hash(token)[:8]
    assert sent[0]["outcome"] == "submitted"


async def test_security_headers_on_all_responses(client, token):
    r200 = await client.get(f"/a2a/{token}/.well-known/agent-card.json")
    r401 = await client.get("/a2a/")
    for resp in (r200, r401):
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"
        assert resp.headers["x-frame-options"] == "SAMEORIGIN"


# ── SSE 进度流（P1-3，2026-11-16）───────────────────────────────────────────


async def test_message_stream_returns_sse_with_status_frames(client, token, deps):
    """message/stream 必须回 text/event-stream 且至少一帧 status。"""
    resp = await client.post(
        f"/a2a/{token}/message/stream",
        json=rpc("message/stream", {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}}),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "event: status" in body
    assert "data: " in body


async def test_message_stream_rejects_wrong_method(client, token):
    """专用路由只认自己的 method（与 tasks/get 同口径）。"""
    resp = await client.post(f"/a2a/{token}/message/stream", json=send_payload("hi"))
    assert resp.status_code == 200
    assert "error" in resp.json()


async def test_tasks_resubscribe_streams_existing_task(client, token, deps):
    """resubscribe 既有任务 → SSE 帧含该 task id。"""
    created = await _send(client, token, "hi")
    tid = created["id"]
    resp = await client.post(
        f"/a2a/{token}/tasks/resubscribe", json=rpc("tasks/resubscribe", {"id": tid})
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert tid in resp.text


async def test_tasks_resubscribe_unknown_task_not_found(client, token):
    with pytest.raises(Exception):
        await client.post(
            f"/a2a/{token}/tasks/resubscribe", json=rpc("tasks/resubscribe", {"id": "nope"})
        )


async def test_sse_requires_auth(client, token):
    """SSE 端点同样受鉴权闸保护（无 token 不得进流）。"""
    bad = "C" * 26
    resp = await client.post(
        f"/a2a/{bad}/message/stream",
        json=rpc("message/stream", {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}}),
    )
    assert resp.status_code in (401, 403)


def test_sse_frame_format():
    """SSE 帧格式：event 行 + data 行 + 空行结尾。"""
    from ginno_runtime.a2a.server import _sse_frame

    frame = _sse_frame("status", {"id": "t1"})
    assert frame.startswith("event: status\n")
    assert '"id": "t1"' in frame or '"id":"t1"' in frame
    assert frame.endswith("\n\n")
