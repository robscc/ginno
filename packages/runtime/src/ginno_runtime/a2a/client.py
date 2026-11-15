"""A2A JSON-RPC 2.0 over HTTP 客户端（设计 §3、§7）。

P0 只做同步三操作：``message/send`` / ``tasks/get`` / ``tasks/cancel``
（method 名核对自上游 a2a.proto；SSE 流式留 P1）。

错误统一转 :class:`A2AError` 抛出——**工具层负责压平成字符串**，本层不吞
错误也不自作主张重试（delegate_agent seam：结果字符串唯一回流，调用方决定
语义，如 cancel 遇 TaskNotFoundError 视为成功，设计 §9.6）。

SSRF 闸在发现/接入层（card.guard_endpoint）：能建 A2AClient 的 endpoint
都已过接入向导；本层不重复 guard，否则内网白名单 peer 无法直连。
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from ginno_runtime import jsonx as json
from ginno_runtime._version import ginno_user_agent

from . import card as _card
from .errors import INTERNAL_ERROR, INVALID_AGENT_RESPONSE, A2AError, error_from_rpc
from .models import Message, Task


class A2AClient:
    """单个远端 agent 的 JSON-RPC 客户端。

    - ``token``：能力地址之外的显式 Bearer 凭据（Authorization 头）；
      能力地址的 token 已在 endpoint path 里，无需重复传；
    - ``api_key``：OpenAPI apiKey 风格的简化落法（X-Api-Key 头）；
    - ``verify_fp``：pinning 模式——信任锚是地址里的指纹而非 CA（对端多为
      自签证书），故自建连接关 CA 校验、首连前独立 TLS 校验指纹，通过后
      才发任何带凭据的请求；注入 http_client 时校验仍做，CA 策略由注入方负责；
    - ``http_client``：注入测试 transport（httpx.MockTransport）；注入时
      不拥有其生命周期，aclose() 只关自建的。
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        api_key: str | None = None,
        verify_fp: str | None = None,
        timeout_s: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        ep = (endpoint or "").strip()
        if "://" not in ep:
            # 裸域名补 https；"javascript:xxx" 伪 scheme 不能当域名（同 address._split）
            if ":" in ep.split("/", 1)[0]:
                raise ValueError(f"仅支持 http/https endpoint，收到 {endpoint!r}")
            ep = "https://" + ep
        if urlsplit(ep).scheme not in ("http", "https") or not urlsplit(ep).hostname:
            raise ValueError(f"仅支持 http/https endpoint，收到 {endpoint!r}")
        self.endpoint = ep
        self._verify_fp = (verify_fp or "").strip()
        self._fp_checked = False
        self._ids = itertools.count(1)  # JSON-RPC id 单调递增，从 1 起
        headers = {"User-Agent": ginno_user_agent(), "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["X-Api-Key"] = api_key
        self._headers = headers
        self._owns_http = http_client is None
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(
                timeout=timeout_s,
                headers=headers,
                verify=not self._verify_fp,
            )

    async def send_message(
        self,
        text_parts: Sequence[str] | str,
        *,
        context_id: str | None = None,
        reference_task_ids: Sequence[str] | None = None,
    ) -> Message | Task:
        """派活/追问。响应是 Message 直接返回；含 task（有 status 字段）返回 Task。

        追问语义（设计 §8.1）：终态任务不可重启，同 context_id 开新 task 并带
        reference_task_ids——contextId 由 server 生成、客户端视为不透明，
        这里只透传，绝不自己造。
        """
        if isinstance(text_parts, str):
            text_parts = [text_parts]
        message: dict[str, Any] = {
            "role": "user",
            "parts": [{"kind": "text", "text": str(t)} for t in text_parts],
            "messageId": uuid.uuid4().hex,
            "kind": "message",
        }
        if context_id:
            message["contextId"] = context_id
        if reference_task_ids:
            message["referenceTaskIds"] = list(reference_task_ids)
        result = await self._rpc("message/send", {"message": message})
        # Task 必有 status；Message 没有——这是 1.0 union 响应的判别键
        if isinstance(result, dict) and "status" in result:
            return Task.from_dict(result)
        return Message.from_dict(result)

    async def get_task(self, task_id: str) -> Task:
        """查任务（纯轮询不续期，设计 §9.6 / 验收 #23）。"""
        result = await self._rpc("tasks/get", {"id": task_id})
        return Task.from_dict(result)

    async def cancel_task(self, task_id: str) -> dict:
        """取消任务，返回 server 的 result 原样 dict。

        幂等语义由调用方落实：purged 后 server MAY 回 TaskNotFoundError，
        客户端应视为取消成功（errors.is_not_found，设计 §9.6 / 验收 #22）。
        """
        result = await self._rpc("tasks/cancel", {"id": task_id})
        return result if isinstance(result, dict) else {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _rpc(self, method: str, params: dict) -> Any:
        """组 JSON-RPC 2.0 请求并规整响应；一切失败抛 A2AError。"""
        if self._verify_fp and not self._fp_checked:
            # 指纹钉住是 **TLS** 概念：明文 http 端点没有证书可校验，
            # 强行握手必然 SSL record layer failure（2026-11-16 实测）。
            # 旧版 node 对 http 也广告 #fp=，这里跳过以兼容已发出的地址；
            # 明文的传输安全由环回/SSH 隧道提供。新地址已在 build_address
            # 侧不再附 fp。https 端点一律照常校验，绝不放宽。
            if urlsplit(self.endpoint).scheme == "https":
                await _card.verify_endpoint_fingerprint(self.endpoint, self._verify_fp)
            self._fp_checked = True
        req_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        try:
            resp = await self._http.post(self.endpoint, json=payload, headers=self._headers)
        except httpx.HTTPError as e:
            raise A2AError(f"A2A 请求失败（网络错误）：{e}") from e
        try:
            body = json.loads(resp.text)
        except (ValueError, TypeError):
            body = None
        if resp.status_code != 200:
            # 有些 server 把 JSON-RPC error 放在非 200 响应里（gRPC/HTTP 映射），
            # 能解析就按协议错误走，保留 -32001 等语义供自愈分支使用
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                raise error_from_rpc(body["error"])
            hint = "（鉴权失败，检查 token/api_key）" if resp.status_code in (401, 403) else ""
            raise A2AError(f"A2A 请求失败：HTTP {resp.status_code}{hint}", code=INTERNAL_ERROR)
        if not isinstance(body, dict):
            raise A2AError("A2A 响应不是合法 JSON-RPC 对象", code=INVALID_AGENT_RESPONSE)
        if isinstance(body.get("error"), dict):
            raise error_from_rpc(body["error"])
        if "result" not in body:
            raise A2AError("A2A 响应缺少 result 字段", code=INVALID_AGENT_RESPONSE)
        resp_id = body.get("id")
        if resp_id is not None and str(resp_id) != str(req_id):
            raise A2AError(
                f"A2A 响应 id 不匹配（{resp_id!r} != {req_id!r}）",
                code=INVALID_AGENT_RESPONSE,
            )
        return body["result"]
