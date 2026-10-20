"""钉钉 OpenAPI HTTP 封装：accessToken / 媒体收发 / 机器人单发（设计 F6/F7/F9）。

设计出处：docs/dingtalk-connector-design.md。

- token 缓存（F1 步骤 4）：accessToken 有效期 7200s，内存缓存、提前 120s 懒刷新；
- 下载机器人收到的文件（F6 步骤 2-3）：downloadCode → 临时 downloadUrl → 流式落盘；
- 媒体上传（F7 步骤 1-2）：multipart 上传得 mediaId（LRU 缓存 path+size+mtime，
  同文件不重传，上限 64）；
- 企业内部机器人单发（F3 降级 / F7 富媒体）：oToMessages/batchSend；
- 全局 20 QPS 令牌桶限流（F9 步骤 3）：上传/发卡/流式更新共用一个闸。

端点与参数均已对照官方 dingtalk-stream-sdk-python（card_replier.py / chatbot.py）
及第三方实现 openclaw-channel-dingtalk 逐字段核验；不确定处标注
``TODO(dingtalk)``，不编造。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.dingtalk.com"
"""新版 OpenAPI 域名（REST，鉴权走 x-acs-dingtalk-access-token header）。"""

OAPI_BASE = "https://oapi.dingtalk.com"
"""旧版 oapi 域名（media/upload 仍在此，鉴权走 access_token query 参数）。"""

# 企业内部机器人单发的 msgKey 常量（官方 robot/oToMessages/batchSend 协议）。
MSG_TEXT = "sampleText"  # msgParam: {"content": str}
MSG_MARKDOWN = "sampleMarkdown"  # msgParam: {"title": str, "text": str}
MSG_IMAGE = "sampleImage"  # msgParam: {"photoURL": str} —— 需公网可访问 URL
MSG_FILE = "sampleFile"  # msgParam: {"mediaId": str, "fileName": str, "fileExtension": str}

_TOKEN_TTL_S = 7200  # accessToken 官方有效期（秒）
_TOKEN_EARLY_REFRESH_S = 120  # 提前刷新窗口：到期前 120s 即视为过期（懒刷新）
_QPS = 20.0  # 全局限流（F9 步骤 3：钉钉 API QPS ~20）
_MEDIA_CACHE_MAX = 64  # mediaId LRU 上限（同文件不重传，F7 步骤 1）
_CHUNK = 1024 * 1024  # 下载写盘分块


class DingTalkApiError(Exception):
    """钉钉 OpenAPI 调用失败。

    ``code``：新版 API 为响应体 ``code`` 字符串（兜底 HTTP 状态码）；
    旧版 oapi 为 ``errcode`` 整数。``message`` 为人读错误信息。
    """

    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def api_error_of(resp: httpx.Response) -> DingTalkApiError:
    """把一个非 2xx 响应解析成 :class:`DingTalkApiError`（新版 API 错误体格式）。"""
    try:
        data = resp.json()
    except ValueError:
        data = {}
    code = data.get("code") or resp.status_code
    message = str(data.get("message") or resp.text[:200] or resp.reason_phrase)
    return DingTalkApiError(code, message)


class _TokenBucket:
    """异步令牌桶（F9 步骤 3）：速率 20 token/s、容量 20，全 client 共享。"""

    def __init__(self, rate: float = _QPS, capacity: float = _QPS) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """取一枚令牌；不足则按速率折算等待（持锁等待 = 请求天然串行，可接受）。"""
        async with self._lock:
            while True:
                now = time.monotonic()
                refill = (now - self._updated) * self._rate
                self._tokens = min(self._capacity, self._tokens + refill)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class DingTalkClient:
    """钉钉 OpenAPI 异步客户端（httpx 全栈，企业内部应用机器人）。

    职责边界：本类只做传输与鉴权（token 缓存 / 限流 / 错误规整化）；
    卡片编排（流式 AI 卡片 / ask_user 互动卡片）见 :mod:`.cards`，
    Stream 接入与消息路由见 adapter（另行实现）。
    """

    def __init__(self, app_key: str, app_secret: str, robot_code: str | None = None) -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        # robotCode 缺省 = appKey（钉钉企业内部应用二者一致；§2.2 config 同款约定）
        self.robot_code = robot_code or app_key
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=120.0))
        self._token = ""
        self._token_expire_at = 0.0  # monotonic 时间戳
        self._token_lock = asyncio.Lock()
        self._bucket = _TokenBucket()
        # mediaId LRU：key=(绝对路径, size, mtime)，文件变了自然 miss（F7 步骤 1）
        self._media_cache: OrderedDict[tuple[str, int, int], str] = OrderedDict()

    # ---- 限流 ------------------------------------------------------------

    @asynccontextmanager
    async def throttle(self) -> AsyncIterator[None]:
        """全局 20 QPS 限流闸：所有 OpenAPI 调用点 ``async with client.throttle():``。"""
        await self._bucket.acquire()
        yield

    # ---- 鉴权 ------------------------------------------------------------

    async def token(self) -> str:
        """获取（或复用缓存的）accessToken（F1 步骤 4）。

        POST /v1.0/oauth2/accessToken（body appKey/appSecret）→ accessToken +
        expireIn(=7200s)。缓存于内存，到期前 120s 懒刷新；并发刷新由锁合并。
        失败抛 :class:`DingTalkApiError`（secret 错误由调用方转 error 状态，
        F1 异常路径：不重试、等用户改配置）。
        """
        if self._token and time.monotonic() < self._token_expire_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expire_at:
                return self._token  # 双检：并发等待期间已被同伴刷新
            async with self.throttle():
                resp = await self._http.post(
                    f"{API_BASE}/v1.0/oauth2/accessToken",
                    json={"appKey": self.app_key, "appSecret": self.app_secret},
                )
            if resp.status_code >= 400:
                raise api_error_of(resp)
            try:
                data = resp.json()
            except ValueError:
                data = {}
            access_token = data.get("accessToken")
            if not access_token:
                raise DingTalkApiError(
                    data.get("code", "InvalidResponse"),
                    f"accessToken 响应缺 accessToken 字段: {data}",
                )
            expire_in = int(data.get("expireIn") or _TOKEN_TTL_S)
            self._token = str(access_token)
            self._token_expire_at = time.monotonic() + expire_in - _TOKEN_EARLY_REFRESH_S
            return self._token

    # ---- 通用请求 ----------------------------------------------------------

    async def api_request(
        self, method: str, path: str, *, json_body: dict | None = None
    ) -> dict:
        """新版 OpenAPI 通用请求：自动带 accessToken + 全局限流。

        ``path`` 为 ``/v1.0/...`` 形式（拼在 :data:`API_BASE` 后）。非 2xx 抛
        :class:`DingTalkApiError`；2xx 返回解析后的 JSON（空体返回 ``{}``）。
        供本类方法与 :mod:`.cards` 的卡片接口复用（单一传输出口）。
        """
        access_token = await self.token()
        async with self.throttle():
            resp = await self._http.request(
                method,
                f"{API_BASE}{path}",
                json=json_body,
                headers={"x-acs-dingtalk-access-token": access_token},
            )
        if resp.status_code >= 400:
            raise api_error_of(resp)
        try:
            return resp.json()
        except ValueError:
            return {}

    # ---- 文件下行（F6） ----------------------------------------------------

    async def download_message_file(self, download_code: str, dest: Path) -> Path:
        """下载机器人接收消息的文件（F6 步骤 2-3）。

        两跳：① POST /v1.0/robot/messageFiles/download（body downloadCode +
        robotCode，header accessToken）→ 响应含临时 ``downloadUrl``（短有效期，
        拿到立即用）；② GET downloadUrl（URL 自带签名，无需再鉴权）流式写盘。
        落盘原子化：先写 ``<dest>.part`` 再 rename，防半截文件被 turn 消费。
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = await self.api_request(
            "POST",
            "/v1.0/robot/messageFiles/download",
            json_body={"downloadCode": download_code, "robotCode": self.robot_code},
        )
        # 兼容顶层 downloadUrl 与 data.downloadUrl 两种响应形态（第三方实现佐证）
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        url = data.get("downloadUrl") or payload.get("downloadUrl")
        if not url:
            raise DingTalkApiError(
                data.get("code", "NoDownloadUrl"),
                f"messageFiles/download 未返回 downloadUrl: {data}",
            )
        part = dest.with_name(dest.name + ".part")
        async with self.throttle():
            async with self._http.stream("GET", str(url)) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise api_error_of(resp)
                with part.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        fh.write(chunk)
        part.replace(dest)
        return dest

    # ---- 媒体上行（F7） ----------------------------------------------------

    async def upload_media(self, path: Path, *, type: str = "file") -> str:
        """上传媒体文件得 mediaId（F7 步骤 1-2）。

        旧版 multipart：POST {OAPI_BASE}/media/upload?access_token=…&type=file
        （multipart 字段名 ``media``，文件名取 basename）→ ``{"errcode": 0,
        "media_id": …}``。``type`` 可选 ``image``。命中 LRU 缓存（path+size+mtime，
        上限 64）直接返回，同文件不重传。图片/视频大小上限 ~20MB 待实测
        （设计 §7.4），超限由调用方降级为路径回执（F7 异常路径）。
        """
        path = Path(path)
        stat = path.stat()
        cache_key = (str(path.resolve()), stat.st_size, int(stat.st_mtime))
        cached = self._media_cache.get(cache_key)
        if cached is not None:
            self._media_cache.move_to_end(cache_key)
            return cached

        access_token = await self.token()
        async with self.throttle():
            with path.open("rb") as fh:
                resp = await self._http.post(
                    f"{OAPI_BASE}/media/upload",
                    params={"access_token": access_token, "type": type},
                    files={"media": (path.name, fh)},
                )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        errcode = data.get("errcode", 0)
        if resp.status_code >= 400 or errcode not in (0, None):
            raise DingTalkApiError(
                errcode if errcode is not None else resp.status_code,
                str(data.get("errmsg") or resp.text[:200]),
            )
        media_id = str(data.get("media_id") or "")
        if not media_id:
            raise DingTalkApiError(
                data.get("errcode", "NoMediaId"),
                f"media/upload 未返回 media_id: {data}",
            )
        self._media_cache[cache_key] = media_id
        if len(self._media_cache) > _MEDIA_CACHE_MAX:
            self._media_cache.popitem(last=False)
        return media_id

    # ---- 机器人单发（F3 降级 / F7 富媒体） ---------------------------------

    async def send_single_message(self, staff_id: str, msg_key: str, msg_param: dict) -> dict:
        """企业内部机器人单聊定向发送（F3 降级分支 / F7 富媒体出口）。

        POST /v1.0/robot/oToMessages/batchSend（body robotCode/userIds/[staff_id]/
        msgKey/msgParam=JSON 字符串）。msgKey 见模块常量：text={content}、
        markdown={title,text}、image={photoURL}（需公网 URL——本地图片应先
        :meth:`upload_media` 再走 ``MSG_FILE``={mediaId,fileName,fileExtension}）。
        成功返回响应 dict（含 processQueryKey）。
        """
        return await self.api_request(
            "POST",
            "/v1.0/robot/oToMessages/batchSend",
            json_body={
                "robotCode": self.robot_code,
                "userIds": [staff_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            },
        )

    # ---- 生命周期 ----------------------------------------------------------

    async def close(self) -> None:
        """关闭底层 httpx 连接池（adapter stop 时调用）。"""
        await self._http.aclose()
