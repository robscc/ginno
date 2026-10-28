"""企业微信智能机器人长连接 WS 客户端（协议传输层）。

职责边界（与 adapter 的分工）：本类只做传输——建连、订阅鉴权、心跳、
req_id 关联的回执等待（request/response）、收帧分发回调与断线报告；
业务路由（白名单/会话映射/turn 驱动）与重连调度（退避、状态上报）在
adapter。重连不放在本类是因为订阅失败（凭证错）需要「不重试、报 error」
的业务判断（同钉钉 F1 的鉴权预检语义）。

回执关联：订阅/心跳/回复/主动推送/上传的 ack 都是
``{headers: {req_id}, errcode, errmsg}``（errcode 在顶层）；同一个
req_id 的多次 respond 由调用方天然串行（await 每次 ack 后才发下一帧）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ginno_runtime import jsonx as json

from . import protocol as p

log = logging.getLogger("ginno.wecom")

_ACK_TIMEOUT_S = 15.0
_MAX_MISSED_PONG = 3


class WecomError(Exception):
    """协议层错误（errcode != 0）。"""

    def __init__(self, errcode: Any, errmsg: str) -> None:
        super().__init__(f"[{errcode}] {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


class WecomAuthError(WecomError):
    """订阅被拒（bot_id/secret 错误）——不重试，等用户改配置。"""


# 收到消息/事件回调的处理器签名（async，adapter 注入；抛异常不影响收帧循环）
MsgHandler = Callable[[dict, dict], Awaitable[None]]
EventHandler = Callable[[dict, dict], Awaitable[None]]


class WecomClient:
    """一条长连接的完整生命周期（连接后 recv 循环阻塞直至断开）。"""

    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        on_msg: MsgHandler | None = None,
        on_event: EventHandler | None = None,
        url: str = p.WSS_URL,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.url = url
        self._on_msg = on_msg
        self._on_event = on_event
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._missed_pong = 0
        self._closed = False  # 主动 close（不需要再连）

    # ---- 连接与订阅 ---------------------------------------------------------

    async def connect_subscribe(self) -> None:
        """建连 + 订阅（凭证错误抛 :class:`WecomAuthError`）。

        成功返回后连接保持在后台 recv 循环里，调用方 await :meth:`run`。
        """
        import websockets  # 延迟 import：与 PyInstaller 懒加载约定一致

        self._closed = False
        self._ws = await websockets.connect(self.url, ping_interval=None,
                                            ping_timeout=None)
        self._missed_pong = 0
        # recv 循环必须先于订阅 ack 的等待启动——否则没人读 socket，
        # ack 永远到不了（request 死锁超时）。
        self._recv_task = asyncio.create_task(self._recv_loop())
        try:
            await self.request(p.CMD_SUBSCRIBE, {"bot_id": self.bot_id,
                                                 "secret": self.secret})
        except WecomError as e:
            await self.close()
            raise WecomAuthError(e.errcode, e.errmsg) from e
        except Exception:
            await self.close()
            raise
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def run(self) -> None:
        """阻塞直到连接断开（recv 循环退出）；供 adapter 的监督循环 await。"""
        if self._recv_task is not None:
            await asyncio.gather(self._recv_task, return_exceptions=True)

    async def close(self) -> None:
        """主动断开：停心跳、清 pending、关 WS。幂等。"""
        self._closed = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._fail_pending("connection closed")
        await self._close_ws()

    async def _close_ws(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 — 对端先断/半开时 close 可能失败
            pass

    # ---- 请求/回执 -----------------------------------------------------------

    async def request(self, cmd: str, body: dict | None = None,
                      req_id: str | None = None, *,
                      timeout_s: float = _ACK_TIMEOUT_S) -> dict:
        """发送一帧并等待同 req_id 的 ack body；errcode!=0 抛 :class:`WecomError`。"""
        f = p.frame(cmd, body, req_id)
        rid = f["headers"]["req_id"]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[rid] = fut
        try:
            async with self._send_lock:
                if self._ws is None:
                    raise ConnectionError("wecom ws not connected")
                await self._ws.send(json.dumps(f, ensure_ascii=False))
            ack_body = await asyncio.wait_for(fut, timeout_s)
            return ack_body
        finally:
            self._pending.pop(rid, None)

    async def respond(self, req_id: str, body: dict) -> dict:
        """回复一条收到的回调（透传其 req_id）。"""
        return await self.request(p.CMD_RESPOND_MSG, body, req_id)

    async def respond_welcome(self, req_id: str, content: str) -> dict:
        return await self.request(
            p.CMD_RESPOND_WELCOME, p.text_body(content), req_id)

    async def send_markdown(self, chatid: str, content: str) -> dict:
        """主动推送 markdown（不依赖回调 req_id；长任务收尾的兜底出口）。"""
        return await self.request(p.CMD_SEND_MSG,
                                  p.markdown_push_body(chatid, content))

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    # ---- 心跳与收帧 -----------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._closed and self._ws is not None:
                await asyncio.sleep(p.HEARTBEAT_S)
                if self._missed_pong >= _MAX_MISSED_PONG:
                    log.warning("wecom heartbeat: %d pongs missed, closing",
                                self._missed_pong)
                    await self._close_ws()
                    return
                self._missed_pong += 1
                try:
                    await self.request(p.CMD_PING)
                except Exception:  # noqa: BLE001 — 单次失败等下一轮计数判断
                    log.warning("wecom heartbeat send failed", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def _recv_loop(self) -> None:
        ws = self._ws
        try:
            while not self._closed and self._ws is not None:
                raw = await ws.recv()
                await self._handle_raw(raw)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — 断线/对端关闭都走这里
            pass
        finally:
            self._fail_pending("wecom connection closed")
            # 心跳任务由 close()/下次建连收尾；这里先停掉防止踩已关连接
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

    async def _handle_raw(self, raw: str | bytes) -> None:
        frame = p.parse_frame(raw)
        if not frame:
            return
        rid = str(frame["headers"].get("req_id") or "")
        if frame["errcode"] is not None:
            # 心跳 ack：恢复计数（cmd 缺省或 ping）
            if frame["cmd"] in (None, p.CMD_PING):
                self._missed_pong = max(0, self._missed_pong - 1)
            fut = self._pending.get(rid)
            if fut is not None and not fut.done():
                errcode = frame["errcode"]
                if errcode == 0:
                    fut.set_result(frame["body"] or {})
                else:
                    fut.set_exception(WecomError(
                        errcode, str(frame["errmsg"] or "wecom error")))
            return
        cmd = str(frame["cmd"] or "")
        body = frame["body"] if isinstance(frame["body"], dict) else {}
        if cmd == p.CMD_MSG_CALLBACK and self._on_msg is not None:
            await self._spawn(self._on_msg(body, frame))
        elif cmd == p.CMD_EVENT_CALLBACK and self._on_event is not None:
            await self._spawn(self._on_event(body, frame))

    @staticmethod
    async def _spawn(coro: Awaitable[None]) -> None:
        """回调独立成 task：一条坏消息/慢消息不阻塞收帧循环（5s 首帧时限）。"""
        task = asyncio.create_task(coro)

        def _done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.warning("wecom callback handler failed",
                            exc_info=t.exception())

        task.add_done_callback(_done)


async def probe_credentials(bot_id: str, secret: str) -> dict:
    """一次性连接测试（POST /api/connectors/wecom-bot/test 用）。

    建连 → 订阅 → 立即断开，不占长连接位。成功返回 ``{"ok": True}``；
    凭证错误抛 :class:`WecomAuthError`，网络问题抛原异常——调用方统一
    转 ``{"ok": False, "error": …}``。
    """
    client = WecomClient(bot_id, secret)
    try:
        await client.connect_subscribe()
        return {"ok": True}
    finally:
        await client.close()
