"""错误契约包封回归（harness 06 轮发现）：connectors 路由的错误响应必须
走 house style `{ok:false, error:...}`——此前 4 处裸 `{error}`（unknown
connector ×2 / body must be… / unknown config keys / unknown action /
domain required），前端统一错误处理读不到 ok 字段会走错分支。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.unit


@pytest.fixture()
def app():
    from ginno_runtime.api.connectors import router

    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture()
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://conn.test"
    ) as c:
        yield c


@pytest.mark.parametrize(
    "method,path,body,status",
    [
        ("GET", "/api/connectors/__nope__", None, 404),
        ("PATCH", "/api/connectors/__nope__/config", {"enabled": True}, 404),
        ("PATCH", "/api/connectors/a2a/config", {}, 400),  # 空对象 → body must be…
        ("POST", "/api/connectors/a2a/action", {"action": "nope"}, 400),
    ],
)
async def test_connector_errors_are_enveloped(client, method, path, body, status):
    resp = await client.request(method, path, json=body)
    assert resp.status_code == status
    data = resp.json()
    assert data.get("ok") is False, f"{path} 错误响应缺少 ok:false 包封"
    assert data.get("error")
