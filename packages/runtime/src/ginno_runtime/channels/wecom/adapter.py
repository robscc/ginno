"""WeComChannelAdapter：企业微信智能机器人长连接对接。

线程模型：纯 asyncio（``websockets`` 原生 async，不需要钉钉那种 SDK 专用
线程桥）。监督循环 ``_supervise`` 负责建连/订阅/退避重连与状态上报；
消息回调由 client 独立成 task 投递（5s 首帧时限），经白名单 → 会话映射
→ ``base.run_channel_turn`` headless 驱动，流式输出由 ``WeComTap`` 续流。
"""

from __future__ import annotations

import asyncio
import random
import time

import httpx

from ... import paths
from ... import server_shared as shared
from ...connectors.registry import STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_ERROR
from ..base import (
    ChannelAdapter,
    ensure_channel_session,
    image_payload_from_bytes,
    mark_channel_sessions,
    run_channel_turn,
    steer_channel_turn,
)
from . import protocol as p
from .client import WecomAuthError, WecomClient
from .sessions import SessionMapper
from .tap import WeComTap

_PLACEHOLDER = "⏳ 已收到，正在处理…"
_BACKOFF_MAX_S = 60.0


class WeComChannelAdapter(ChannelAdapter):
    """企微智能机器人适配器：Connector(配置/状态) 与 Ginno 核心之间的桥。"""

    channel_name = "wecom-bot"

    def __init__(self) -> None:
        super().__init__()
        self.config: dict = {}
        self.client: WecomClient | None = None
        self._mapper: SessionMapper | None = None
        self._http: httpx.AsyncClient | None = None
        self._supervise_task: asyncio.Task | None = None
        self._stopped = True
        self._session_locks: dict[str, asyncio.Lock] = {}  # 在途 turn 串行
        # chat_key → {session_id, question_id, options}：ask_user 停车时，
        # 该会话的下一条文本消息按「作答」处理而不是开新 turn
        self._pending_q: dict[str, dict] = {}

    # ---- 生命周期 -----------------------------------------------------------

    def _make_client(self) -> WecomClient:
        return WecomClient(
            self.config["bot_id"], self.config["secret"],
            on_msg=self.handle_msg, on_event=self.handle_event,
        )

    async def start(self, config: dict) -> None:
        self.config = config
        self.report_status(STATUS_DISCONNECTED, "Starting")
        data_dir = paths.home() / "channels" / "wecom"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._mapper = SessionMapper(data_dir / "mappings.json")
        self._mapper.load()
        # 存量迁移：标记引入前绑定的会话补 type/channel（幂等，见 base.py）
        mark_channel_sessions(
            "wecom", [m["ginno_session"] for m in self._mapper.all()])
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=60.0))
        self.client = self._make_client()
        self._stopped = False
        self._supervise_task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        """建连 → 订阅 → 阻塞到断线 → 退避重连；凭证错误直接 error 不重试。"""
        backoff = 1.0
        while not self._stopped:
            client = self.client
            assert client is not None
            connected_at = time.monotonic()
            try:
                await client.connect_subscribe()
                self.report_status(STATUS_CONNECTED, "Long connection subscribed")
                await client.run()
            except WecomAuthError as e:
                self.report_status(STATUS_ERROR, f"Credential check failed: {e}")
                return
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 — 网络类问题一律退避重试
                if self._stopped:
                    return
                self.report_status(STATUS_DISCONNECTED, f"{e}; reconnecting…")
            if self._stopped:
                return
            self.report_status(STATUS_DISCONNECTED, "Connection lost; reconnecting…")
            # 稳定跑过 60s 的连接重置退避；否则按 1/2/4/…/60s 退避 + 抖动
            if time.monotonic() - connected_at > 60:
                backoff = 1.0
            await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
            self.client = self._make_client()  # 换新实例，旧 pending 已被清

    async def stop(self) -> None:
        self._stopped = True
        if self._supervise_task is not None:
            self._supervise_task.cancel()
            self._supervise_task = None
        if self.client is not None:
            await self.client.close()
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        self.report_status(STATUS_DISCONNECTED, "Stopped")

    # ---- 消息路由 -----------------------------------------------------------

    async def handle_msg(self, body: dict, frame: dict) -> None:
        """aibot_msg_callback → 白名单 → 待答短路 → 文本/媒体分发。"""
        userid = p.msg_user_id(body)
        if not userid:
            return
        shared._log.info(
            "wecom_msg chat=%s type=%s chattype=%s user=%s",
            p.msg_chat_key(body), body.get("msgtype"), body.get("chattype"), userid)
        allow: list[str] = self.config.get("allowed_userids") or []
        if allow and userid not in allow:
            return  # 未授权静默忽略（防探测，同钉钉 F11）
        req_id = str(frame["headers"].get("req_id") or "")
        chat_key = p.msg_chat_key(body)
        msgtype = str(body.get("msgtype") or "")

        if msgtype == "text":
            raw = p.extract_text(body)
            # 频道（话题）全量推送：只回应正文 @机器人 的那条；群聊只推被 @ 的
            if p.msg_is_channel(body) and not p.is_mention_text(raw):
                return
            text = p.strip_mention_prefix(raw).strip()
            if not text:
                return
            if await self._try_answer(req_id, chat_key, text):
                return
            await self._dispatch_text(req_id, chat_key, userid, text)
        elif msgtype in ("image", "file"):
            await self._dispatch_media(req_id, chat_key, userid, body, msgtype)
        elif msgtype == "voice":
            await self._dispatch_voice(req_id, chat_key, userid, body)
        elif msgtype == "mixed":
            text, image = p.extract_mixed(body)
            await self._dispatch_media(req_id, chat_key, userid, body, "mixed",
                                       extra_text=text, image=image)
        # voice/video：P1 暂不消费（语音转写待 P2）

    async def _try_answer(self, req_id: str, chat_key: str, text: str) -> bool:
        """ask_user 停车中的会话：该消息按作答 resume，而非新 turn。"""
        pending = self._pending_q.get(chat_key)
        if pending is None:
            return False
        if pending["session_id"] not in shared._PENDING_RESUME:
            # 问题已超时/已被消费：清登记，按普通消息继续
            self._pending_q.pop(chat_key, None)
            return False
        from ...api.stream.resume import resume_pending_interrupt

        self._pending_q.pop(chat_key, None)
        resume_pending_interrupt(
            pending["session_id"], p.parse_answer(text, pending["options"]))
        return True

    async def handle_event(self, body: dict, frame: dict) -> None:
        """aibot_event_callback：enter_chat 欢迎语（5s 内）；其余 P2 忽略。"""
        event = body.get("event")
        eventtype = str((event or {}).get("eventtype") or "") if isinstance(event, dict) else ""
        req_id = str(frame["headers"].get("req_id") or "")
        if eventtype == "enter_chat" and req_id and self.client is not None:
            welcome = str(self.config.get("welcome_text") or "").strip()
            if welcome:
                try:
                    await self.client.respond_welcome(req_id, welcome)
                except Exception:  # noqa: BLE001 — 欢迎语失败不影响主流程
                    pass
        # template_card_event / feedback_event / topic_*: P2

    # ---- 会话与 turn ----------------------------------------------------------

    async def _begin_stream(self, req_id: str) -> str:
        """5 秒首帧时限：受理即发占位流（返回 stream_id 供 tap 续流）。"""
        stream_id = p.new_req_id("stream")
        try:
            if self.client is not None:
                await self.client.respond(
                    req_id, p.stream_body(stream_id, _PLACEHOLDER))
        except Exception:  # noqa: BLE001 — 占位失败不拦 turn，终态仍可定格
            pass
        return stream_id

    async def _finish_stream(self, req_id: str, stream_id: str, text: str) -> None:
        try:
            if self.client is not None:
                await self.client.respond(
                    req_id, p.stream_body(stream_id, text, finish=True))
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_session(self, chat_key: str, userid: str) -> dict | None:
        assert self._mapper is not None
        # per_conversation：整个会话（群/频道/单聊）共用一个 session
        map_user = "" if self.config.get("session_policy") == "per_conversation" else userid
        ginno_sid = self._mapper.get(user_id=map_user, chat_id=chat_key)
        session, err = await ensure_channel_session(
            ginno_sid, default_agent_id=self.config.get("default_agent_id") or None,
            title=f"企业微信 · {userid}", channel="wecom")
        if session is None:
            shared._log.error("wecom_session_resolve chat=%s err=%s", chat_key, err)
            return None
        self._mapper.bind(user_id=map_user, chat_id=chat_key,
                          ginno_session=session["session_id"], nick=userid)
        return session

    async def notify_session_deleted(self, session_id: str) -> None:
        """桌面端删除渠道会话 → 解绑全部映射并尽力告知对应 chat。"""
        assert self._mapper is not None
        chats = self._mapper.chats_for(session_id)
        if not chats:
            return
        self._mapper.unbind(session_id)
        for c in chats:
            try:
                if self.client is not None:
                    await self.client.send_markdown(
                        c["chat_id"],
                        "🗑️ 绑定的 Ginno 会话已在桌面端删除，下一条消息将开启新会话。")
            except Exception:  # noqa: BLE001 — 通知失败不影响解绑
                shared._log.warning("wecom_delete_notice chat=%s failed", c["chat_id"])

    async def notify_turn_reply(self, session_id: str, text: str) -> None:
        """无 tap 的 turn（子代理回传 / worker digest / 任务完成注入）收口
        补推：绑定该会话的全部 chat 各推一条 markdown（channels.base.
        relay_channel_reply 的企微侧出口）。📬 前缀标识这是主动推送而非
        对某条消息的应答。"""
        assert self._mapper is not None
        text = (text or "").strip()
        if not text:
            return
        chats = self._mapper.chats_for(session_id)
        if not chats:
            return
        body = f"📬 {text[:4000]}"
        for c in chats:
            try:
                if self.client is not None:
                    await self.client.send_markdown(c["chat_id"], body)
            except Exception:  # noqa: BLE001 — 单 chat 失败不影响其余
                shared._log.warning(
                    "wecom_reply_relay chat=%s failed", c["chat_id"])

    async def _dispatch_text(self, req_id: str, chat_key: str, userid: str,
                             text: str) -> None:
        stream_id = await self._begin_stream(req_id)
        session = await self._resolve_session(chat_key, userid)
        if session is None:
            await self._finish_stream(req_id, stream_id, "⚠️ 会话创建失败，请稍后重试")
            return
        await self._run_text_turn(req_id, chat_key, session, stream_id, text)

    async def _run_text_turn(self, req_id: str, chat_key: str, session: dict,
                             stream_id: str, text: str,
                             *, usage_source: str = "wecom") -> None:
        """文本/语音共用的 turn 驱动：在途 steer → 串行锁 → turn → 补投。"""
        lock = self._session_locks.setdefault(session["session_id"], asyncio.Lock())
        if lock.locked():  # 在途/停车中 → steer 优先，其余不排队直接定格提示
            if steer_channel_turn(session, text):
                await self._finish_stream(
                    req_id, stream_id, "↩️ 已追加到当前任务，本轮继续时生效。")
                return
            await self._finish_stream(
                req_id, stream_id, "⏳ 上一轮仍在进行中（或有问题等你作答），请稍候再发。")
            return
        async with lock:
            tap = WeComTap(self, req_id, chat_key, stream_id)
            salvaged = await run_channel_turn(session, text, tap,
                                              usage_source=usage_source)
        if salvaged:
            await self._salvage_redeliver(session, chat_key, salvaged)

    async def _dispatch_voice(self, req_id: str, chat_key: str, userid: str,
                              body: dict) -> None:
        """语音下行：下载解密 → 千问云 ASR 转写 → 按转写文本驱动 turn。

        转写走录音文件转写（chat completions + input_audio data URL，见
        asr.py；连接器 asr_* 配置）。未配置时定格提示而不是静默（用户至
        少知道语音被收到了）。5s 首帧时限由 _begin_stream 的占位流保证，
        转写耗时不受它约束。
        """
        from . import media as media_mod
        from .asr import transcribe_audio

        stream_id = await self._begin_stream(req_id)
        info = body.get("voice") or {}
        url = str(info.get("url") or "")
        base_url = str(self.config.get("asr_base_url") or "").strip()
        api_key = str(self.config.get("asr_api_key") or "").strip()
        model = str(self.config.get("asr_model") or "").strip()
        if not url:
            await self._finish_stream(req_id, stream_id,
                                      "⚠️ 语音消息缺少媒体地址，无法转写。")
            return
        if not (base_url and api_key and model):
            await self._finish_stream(
                req_id, stream_id,
                "🎙️ 已收到语音；尚未配置语音转写（设置 → 连接器 → 企业微信 "
                "→ ASR Base URL / API Key / Model），请改用文本。")
            return
        fmt = str(info.get("format") or "amr")
        session = await self._resolve_session(chat_key, userid)
        if session is None:
            await self._finish_stream(req_id, stream_id, "⚠️ 会话创建失败，请稍后重试")
            return
        try:
            data, _ = await media_mod.download_media(url, info.get("aeskey"),
                                                     self._http)
            text = await transcribe_audio(
                data, fmt=fmt, base_url=base_url, api_key=api_key,
                model=model, http=self._http)
        except Exception as e:  # noqa: BLE001 — 下载/转写失败定格提示
            shared._log.warning("wecom_voice_transcribe chat=%s err=%s", chat_key, e)
            await self._finish_stream(req_id, stream_id,
                                      "⚠️ 语音转写失败，请改用文本或重试。")
            return
        if not text:
            await self._finish_stream(
                req_id, stream_id, "🎙️ 语音转写结果为空，请重试或改用文本。")
            return
        shared._log.info("wecom_voice_ok chat=%s fmt=%s len=%d text=%r",
                         chat_key, fmt, len(text), text[:80])
        await self._run_text_turn(req_id, chat_key, session, stream_id, text,
                                  usage_source="wecom_voice")

    async def _salvage_redeliver(self, session: dict, chat_key: str,
                                 texts: list[str]) -> None:
        """turn 收尾时未吸收的追加 → 主动告知 + push_only tap 转新 turn 补投
        （无回调流可用，结果走 send_markdown）。第二级不再递归，还文本请重发。"""
        joined = "\n".join(texts)
        try:
            await self.client.send_markdown(
                chat_key,
                f"↩️ 上一轮恰在收尾，追加未注入，已自动转为新任务：\n\n{joined}")
        except Exception:  # noqa: BLE001 — 通知失败不拦补投
            shared._log.warning("wecom_salvage_notice chat=%s failed", chat_key)
        lock = self._session_locks.setdefault(session["session_id"], asyncio.Lock())
        async with lock:  # 已释放，重取（期间新消息可能插队，串行即可）
            tap = WeComTap.push_only(self, chat_key)
            round2 = await run_channel_turn(session, joined, tap,
                                            usage_source="wecom_salvage")
        if round2:  # 补投 turn 又收到追加：不再递归，还文本请重发
            try:
                await self.client.send_markdown(
                    chat_key,
                    "⚠️ 补投任务也刚收尾，追加未能注入，请重新发送：\n\n"
                    + "\n".join(round2))
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch_media(self, req_id: str, chat_key: str, userid: str,
                              body: dict, msgtype: str, *,
                              extra_text: str = "",
                              image: dict | None = None) -> None:
        """文件/图片下行：下载解密 → 会话文件目录 → attached_files → turn。"""
        from . import media as media_mod

        stream_id = await self._begin_stream(req_id)
        media_info: dict = {}
        if msgtype in ("image", "file"):
            media_info = body.get(msgtype) or {}
        elif image is not None:
            media_info = image
        url = str(media_info.get("url") or "")
        aeskey = media_info.get("aeskey")
        if not url:
            await self._finish_stream(req_id, stream_id,
                                      "⚠️ 该消息类型暂不支持，请直接发送文本/图片/文件。")
            return
        session = await self._resolve_session(chat_key, userid)
        if session is None:
            await self._finish_stream(req_id, stream_id, "⚠️ 会话创建失败，请稍后重试")
            return
        sid = session["session_id"]
        slug = session.get("project_slug") or "default"
        msgid = str(body.get("msgid") or "")[:8] or "media"
        kind = "image" if (msgtype == "image" or msgtype == "mixed" and image) else "file"
        ext = {"image": "png", "file": "bin", "mixed": "png"}.get(msgtype, "bin")
        fname = f"wecom_{msgid}.{ext}"
        attached: list[dict] = []
        images: list[dict] | None = None
        try:
            data, cd_fname = await media_mod.download_media(url, aeskey, self._http)
            if cd_fname:
                fname = cd_fname
            dest_dir = paths.session_files_dir(slug, sid)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / fname
            dest.write_bytes(data)
            attached = [{"name": fname, "kind": kind, "path": str(dest)}]
            # 图片同框多模态注入（composer 同款）：模型直接看到像素；文件
            # 落盘照旧（工具路径不受影响）。不可识别格式/超限 → None 回落。
            if kind == "image":
                images = image_payload_from_bytes(data, fname)
        except Exception as e:  # noqa: BLE001 — 下载失败提示但继续（文本仍可驱动）
            shared._log.warning("wecom_media_download chat=%s err=%s", chat_key, e)

        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        if lock.locked():
            await self._finish_stream(
                req_id, stream_id, "⏳ 上一轮仍在进行中（或有问题等你作答），请稍候再发。")
            return
        text = extra_text.strip() or (
            f"我通过企业微信发来了{'图片' if kind == 'image' else '文件'} "
            f"{fname}，请按需处理。"
            if attached
            else f"我通过企业微信发来了{'图片' if kind == 'image' else '文件'}，"
                 "但下载失败（文件不存在，请直接告知用户稍后重试）。")
        async with lock:
            tap = WeComTap(self, req_id, chat_key, stream_id)
            salvaged = await run_channel_turn(
                session, text, tap, attached_files=attached or None,
                images=images, usage_source="wecom_file")
        if salvaged:
            # 文件 turn 收尾时未吸收的文本 steer 同样补投（同 _dispatch_text）
            await self._salvage_redeliver(session, chat_key, salvaged)

    # ---- ask_user 登记（tap 调用）---------------------------------------------

    async def register_pending_question(self, chat_key: str, session_id: str,
                                        question_id: str, options: list) -> None:
        self._pending_q[chat_key] = {
            "session_id": session_id, "question_id": question_id,
            "options": options,
        }

    async def unregister_pending_question(self, chat_key: str) -> None:
        self._pending_q.pop(chat_key, None)
