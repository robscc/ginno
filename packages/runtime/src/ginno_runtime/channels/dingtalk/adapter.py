"""DingTalkChannelAdapter：钉钉 Stream 协议对接（设计 F1/F2/F4/F9/F10）。

线程模型：``dingtalk_stream`` SDK 在独立线程跑自己的 event loop
（``start_forever`` 阻塞）；handler 通过 ``run_coroutine_threadsafe`` 把
消息切回 sidecar 主 loop 处理——Ginno 共享状态（``_SESSION_WS`` /
``_RUNNING_TURNS`` / checkpointer）只在主 loop 触碰。turn 驱动用
``base.run_channel_turn``（headless + 保持 interactive），恢复用
``api/stream/resume.py`` 公共入口。
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from ginno_runtime import jsonx as json

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
from . import menus as menus_mod
from .sessions import SessionMapper
from .tap import DingTalkTap

_MSG_TEXT = "sampleText"
_MSG_MARKDOWN = "sampleMarkdown"


class DingTalkChannelAdapter(ChannelAdapter):
    """钉钉机器人适配器：Connector(配置/状态) 与 Ginno 核心(会话/turn) 之间的桥。"""

    channel_name = "dingtalk-bot"

    def __init__(self) -> None:
        super().__init__()
        self.config: dict = {}
        self.api: Any = None          # media.DingTalkClient（it_2 契约）
        self.cards: Any = None        # cards.CardClient（it_2 契约）
        self._mapper: SessionMapper | None = None
        self._menus: list[menus_mod.MenuItem] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sdk_thread: threading.Thread | None = None
        self._stopped = True
        self._session_locks: dict[str, asyncio.Lock] = {}   # F9 per-session 串行
        self._q2session: dict[str, str] = {}                # question_id → session_id
        self._q_lock = asyncio.Lock()

    # ---- F1 生命周期 -------------------------------------------------------

    async def start(self, config: dict) -> None:
        from .cards import CardClient
        from .media import DingTalkClient

        self.config = config
        self._loop = asyncio.get_running_loop()
        self.report_status(STATUS_DISCONNECTED, "Starting")
        # 鉴权预检：appKey/secret 错误立即报 error，不进重连循环
        self.api = DingTalkClient(config["app_key"], config["app_secret"],
                                  config.get("robot_code") or None)
        try:
            await self.api.token()
        except Exception as e:
            self.report_status(STATUS_ERROR, f"Credential check failed: {e}")
            return
        self.cards = CardClient(self.api, config.get("card_template_id") or None)
        self._menus = menus_mod.load_menus(config)
        data_dir = paths.home() / "channels" / "dingtalk"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._mapper = SessionMapper(data_dir / "mappings.json")
        self._mapper.load()
        # 存量迁移：标记引入前绑定的会话补 type/channel（幂等，见 base.py）
        mark_channel_sessions(
            "dingtalk", [m["ginno_session"] for m in self._mapper.all()])

        try:
            import dingtalk_stream  # 延迟到启动：未装依赖不至于 import 期炸掉
        except ImportError:
            self.report_status(STATUS_ERROR,
                               'Missing dependency: pip install dingtalk-stream')
            return

        adapter = self

        class _BotHandler(dingtalk_stream.ChatbotHandler):
            async def async_process(self, callback: Any) -> tuple[str, str]:
                data = getattr(callback, "data", {}) or {}
                fut = asyncio.run_coroutine_threadsafe(
                    adapter.handle_bot_message(dict(data)), adapter._loop)
                try:
                    fut.result(timeout=10)
                except Exception:
                    adapter.report_status(STATUS_DISCONNECTED, "handler dispatch failed")
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        class _CardHandler(dingtalk_stream.CardCallbackHandler):
            async def async_process(self, callback: Any) -> tuple[str, str]:
                data = getattr(callback, "data", {}) or {}
                fut = asyncio.run_coroutine_threadsafe(
                    adapter.handle_card_callback(dict(data)), adapter._loop)
                try:
                    fut.result(timeout=10)
                except Exception:
                    pass
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        credential = dingtalk_stream.Credential(
            client_id=config["app_key"], client_secret=config["app_secret"])
        client = dingtalk_stream.DingTalkStreamClient(credential, is_async=True)
        client.register_callback_handler(
            dingtalk_stream.ChatbotHandler.TOPIC, _BotHandler())
        client.register_callback_handler(
            dingtalk_stream.CardCallbackHandler.TOPIC, _CardHandler())
        self._stopped = False
        self._sdk_thread = threading.Thread(
            target=self._run_sdk, args=(client,), daemon=True,
            name="dingtalk-stream")
        self._sdk_thread.start()
        self.report_status(STATUS_CONNECTED, "Stream connected")

    def _run_sdk(self, client: Any) -> None:
        """SDK 线程体：start_forever 阻塞；退出时只在仍启用中报 disconnected（F10）。"""
        try:
            client.start_forever()
        except Exception:
            pass
        if not self._stopped:
            # SDK 内部已带断线重连；这里只反映「当前掉线」事实，状态点亮由防抖兜住
            self._safe_report(STATUS_DISCONNECTED, "Stream closed; SDK re-connecting")

    def _safe_report(self, status: str, detail: str) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.report_status, status, detail)
        else:
            self.report_status(status, detail)

    async def stop(self) -> None:
        self._stopped = True
        # SDK 线程为 daemon：置 flag 后由下次进程退出回收；HTTP 侧优雅关闭。
        if self.api is not None:
            try:
                await self.api.close()
            except Exception:
                pass
        self.report_status(STATUS_DISCONNECTED, "Stopped")
        self._sdk_thread = None

    # ---- F2 消息路由 -------------------------------------------------------

    async def handle_bot_message(self, data: dict) -> None:
        """SDK bot 消息 → 白名单 → 菜单 → 会话映射 → 附件 → headless turn。

        必须快速返回（SDK 等待 ack）：turn 本身 spawn 为后台 task。
        """
        staff_id = str(data.get("senderStaffId") or "")
        conversation_id = str(data.get("conversationId") or "")
        nick = str(data.get("senderNick") or "")
        if not staff_id:
            return
        allow: list[str] = self.config.get("allowed_staff_ids") or []
        if allow and staff_id not in allow:
            return  # F11：未授权静默忽略（防探测）
        msgtype = str(data.get("msgtype") or "")
        if msgtype != "text":
            await self._handle_attachment_message(data, staff_id, conversation_id)
            return
        text = str(((data.get("text") or {}).get("content")) or "").strip()
        if not text:
            return
        # F8 菜单 / 内置命令
        if menus_mod.is_builtin(text):
            await self.send_markdown(
                staff_id, "Ginno 菜单", menus_mod.help_markdown(self._menus))
            return
        item = menus_mod.match(self._menus, text)
        usage_source = "dingtalk"
        if item is not None:
            text = menus_mod.render(item, staff_name=nick)
            usage_source = "dingtalk_menu"
        await self._dispatch_turn(
            staff_id, conversation_id, nick, text, [], usage_source,
            force_new=bool(item and item.new_session))

    async def _handle_attachment_message(self, data: dict, staff_id: str,
                                         conversation_id: str) -> None:
        """F6 文件下行：downloadCode → 会话文件目录 → attached_files → turn。"""
        from ... import paths as _paths

        code = str(data.get("downloadCode") or "")
        if not code:
            await self.send_markdown(staff_id, "Ginno",
                                     "（P0 暂支持文本与图片/文件附件；该消息类型已忽略）")
            return
        session = await self._resolve_session(staff_id, conversation_id, "", force_new=False)
        if session is None:
            await self.send_markdown(staff_id, "Ginno", "⚠️ 会话创建失败，请稍后重试")
            return
        sid = session["session_id"]
        slug = session.get("project_slug") or "default"
        dest_dir = _paths.session_files_dir(slug, sid)
        dest_dir.mkdir(parents=True, exist_ok=True)
        fname = str(data.get("fileName") or f"dingtalk_{code[:8]}")
        dest = dest_dir / fname
        try:
            await self.api.download_message_file(code, dest)
        except Exception as e:
            await self.send_markdown(staff_id, "Ginno", f"⚠️ 文件下载失败: {e}")
            return
        kind = "image" if str(data.get("msgtype")) == "picture" else "file"
        attached = [{"name": fname, "kind": kind, "path": str(dest)}]
        # 图片同框多模态注入（composer 同款）：模型直接看到像素；文件落盘
        # 照旧。下载/读取失败或格式不可识别 → None 回落纯附件行为。
        images: list[dict] | None = None
        if kind == "image":
            try:
                images = image_payload_from_bytes(dest.read_bytes(), fname)
            except OSError:
                images = None
        # 附件消息驱动一条「处理该文件」的 turn（正文由 Agent 判断怎么读）
        await self._dispatch_turn(
            staff_id, conversation_id, str(data.get("senderNick") or ""),
            f"我通过钉钉发来了文件 {fname}，请按需处理。", attached, "dingtalk_file",
            force_new=False, images=images)

    async def _resolve_session(self, staff_id: str, conversation_id: str,
                               nick: str, *, force_new: bool) -> dict | None:
        assert self._mapper is not None
        ginno_sid = None if force_new else self._mapper.get(
            staff_id=staff_id, conversation_id=conversation_id)
        session, err = await ensure_channel_session(
            ginno_sid, default_agent_id=self.config.get("default_agent_id") or None,
            channel="dingtalk")
        if session is None:
            shared._log.error("dingtalk_session_resolve staff=%s err=%s", staff_id, err)
            return None
        self._mapper.bind(staff_id=staff_id, conversation_id=conversation_id,
                          ginno_session=session["session_id"], nick=nick)
        return session

    async def notify_session_deleted(self, session_id: str) -> None:
        """桌面端删除渠道会话 → 解绑全部映射并尽力告知对应成员。

        单聊 API 只认 staff_id，群聊绑定的映射也投到成员单聊（足够达意）；
        同一成员多条映射去重，避免重复轰炸。
        """
        assert self._mapper is not None
        chats = self._mapper.chats_for(session_id)
        if not chats:
            return
        self._mapper.unbind(session_id)
        seen: set[str] = set()
        for c in chats:
            staff = c["staff_id"]
            if staff in seen:
                continue
            seen.add(staff)
            try:
                await self.send_markdown(
                    staff, "Ginno",
                    "🗑️ 绑定的 Ginno 会话已在桌面端删除，下一条消息将开启新会话。")
            except Exception:  # noqa: BLE001 — 通知失败不影响解绑
                shared._log.warning("dingtalk_delete_notice staff=%s failed", staff)

    async def notify_turn_reply(self, session_id: str, text: str) -> None:
        """无 tap 的 turn（子代理回传 / worker digest / 任务完成注入）收口
        补推（channels.base.relay_channel_reply 的钉钉侧出口）。同删除通知
        的投递口径：群聊绑定也投成员单聊，去重防轰炸。"""
        assert self._mapper is not None
        text = (text or "").strip()
        if not text:
            return
        chats = self._mapper.chats_for(session_id)
        if not chats:
            return
        body = f"📬 {text[:4000]}"
        seen: set[str] = set()
        for c in chats:
            staff = c["staff_id"]
            if staff in seen:
                continue
            seen.add(staff)
            try:
                await self.send_markdown(staff, "Ginno", body)
            except Exception:  # noqa: BLE001 — 单成员失败不影响其余
                shared._log.warning("dingtalk_reply_relay staff=%s failed", staff)

    async def _dispatch_turn(self, staff_id: str, conversation_id: str, nick: str,
                             text: str, attached: list[dict], usage_source: str,
                             *, force_new: bool,
                             images: list[dict] | None = None) -> None:
        session = await self._resolve_session(staff_id, conversation_id, nick,
                                              force_new=force_new)
        if session is None:
            await self.send_markdown(staff_id, "Ginno", "⚠️ 会话创建失败，请稍后重试")
            return
        sid = session["session_id"]
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        if lock.locked():  # F9：在途 turn/停车中 → steer 优先，其余不排队提示
            # 附件消息不 steer（桌面 v1 同口径，steering-design.md §3.1）：
            # 文本会引用附件，只注入文字会误导模型。
            if not attached and steer_channel_turn(session, text):
                await self.send_markdown(
                    staff_id, "Ginno", "↩️ 已追加到当前任务，本轮继续时生效。")
                return
            await self.send_markdown(
                staff_id, "Ginno", "⏳ 上一轮仍在进行中（或有问题等你作答），请稍候再发。")
            return
        async with lock:
            tap = DingTalkTap(self, staff_id, conversation_id)
            salvaged = await run_channel_turn(
                session, text, tap, attached_files=attached or None,
                images=images, usage_source=usage_source)
        if salvaged:
            # 上一轮在最终 superstep 后收尾、追加没注入成：主动告知并转新
            # turn 补投（锁已释放，_dispatch_turn 重取；新 tap 主动建卡）。
            # 补投 turn 又收到追加则不再递归（usage_source 标记级数），还文
            # 本请重发。
            joined = "\n".join(salvaged)
            if usage_source == "dingtalk_salvage":
                await self.send_markdown(
                    staff_id, "Ginno",
                    f"⚠️ 补投任务也刚收尾，追加未能注入，请重新发送：\n\n{joined}")
                return
            await self.send_markdown(
                staff_id, "Ginno",
                f"↩️ 上一轮恰在收尾，追加未注入，已自动转为新任务：\n\n{joined}")
            await self._dispatch_turn(
                staff_id, conversation_id, nick, joined, [],
                "dingtalk_salvage", force_new=False)

    # ---- F4 卡片回调 -------------------------------------------------------

    async def register_question(self, question_id: str, session_id: str) -> None:
        async with self._q_lock:
            self._q2session[question_id] = session_id

    async def unregister_question(self, question_id: str) -> None:
        async with self._q_lock:
            self._q2session.pop(question_id, None)

    async def handle_card_callback(self, data: dict) -> None:
        """按钮回调 → resume_pending_interrupt（与桌面 WS 同一协议，F4 步骤 3）。"""
        from ...api.stream.resume import resume_pending_interrupt

        raw = str((data.get("content") or {}).get("value")
                  if isinstance(data.get("content"), dict) else data.get("content") or "")
        try:
            val = json.loads(raw) if raw.strip().startswith("{") else {}
        except json.JSONDecodeError:
            return
        qid = str(val.get("q") or "")
        async with self._q_lock:
            sid = self._q2session.pop(qid, None)
        if sid is None:
            return
        act = str(val.get("act") or "opt")
        if act == "skip":
            resume = {"kind": "user_answer", "answer": "", "option_index": None, "skip": True}
        else:
            idx = val.get("i")
            answer = str(val.get("text") or "").strip()
            if not answer and isinstance(idx, int):
                # 选项文本由钉钉按钮带入或省略：resume 契约以 answer 为准，
                # option_index 供 ask_tools 分支判断（见 ask_tools.py 恢复处理）。
                answer = str(val.get("label") or "")
            resume = {"kind": "user_answer", "answer": answer,
                      "option_index": idx if isinstance(idx, int) else None, "skip": False}
        resume_pending_interrupt(sid, resume)
        try:
            await self.cards.disable_question_card(qid)
        except Exception:
            pass

    # ---- 渠道发送便捷方法 ---------------------------------------------------

    async def send_markdown(self, staff_id: str, title: str, text: str) -> None:
        """独立 markdown 消息（降级通道 / 提示类）。"""
        await self.api.send_single_message(staff_id, _MSG_MARKDOWN,
                                           {"title": title or "Ginno", "text": text})

    async def send_media(self, staff_id: str, media_id: str, *, file_name: str,
                         file_ext: str, is_image: bool) -> None:
        """F7 产物上行：图片走 picture，其余走 file 消息。"""
        if is_image:
            # TODO(dingtalk): sampleImage 需公网 URL；本地上传的 mediaId 走
            # sampleFile 通道在部分客户端才可预览——P0 统一 file 消息（可预览）。
            await self.api.send_single_message(staff_id, "sampleFile", {
                "mediaId": media_id, "fileName": file_name, "fileExtension": file_ext})
        else:
            await self.api.send_single_message(staff_id, "sampleFile", {
                "mediaId": media_id, "fileName": file_name, "fileExtension": file_ext})

    async def send_text(self, staff_id: str, text: str) -> None:
        await self.api.send_single_message(staff_id, _MSG_TEXT, {"content": text})
