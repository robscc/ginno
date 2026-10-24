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
from ..base import ChannelAdapter, ensure_channel_session, run_channel_turn
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
        # 附件消息驱动一条「处理该文件」的 turn（正文由 Agent 判断怎么读）
        await self._dispatch_turn(
            staff_id, conversation_id, str(data.get("senderNick") or ""),
            f"我通过钉钉发来了文件 {fname}，请按需处理。", attached, "dingtalk_file",
            force_new=False)

    async def _resolve_session(self, staff_id: str, conversation_id: str,
                               nick: str, *, force_new: bool) -> dict | None:
        assert self._mapper is not None
        ginno_sid = None if force_new else self._mapper.get(
            staff_id=staff_id, conversation_id=conversation_id)
        session, err = await ensure_channel_session(
            ginno_sid, default_agent_id=self.config.get("default_agent_id") or None)
        if session is None:
            shared._log.error("dingtalk_session_resolve staff=%s err=%s", staff_id, err)
            return None
        self._mapper.bind(staff_id=staff_id, conversation_id=conversation_id,
                          ginno_session=session["session_id"], nick=nick)
        return session

    async def _dispatch_turn(self, staff_id: str, conversation_id: str, nick: str,
                             text: str, attached: list[dict], usage_source: str,
                             *, force_new: bool) -> None:
        session = await self._resolve_session(staff_id, conversation_id, nick,
                                              force_new=force_new)
        if session is None:
            await self.send_markdown(staff_id, "Ginno", "⚠️ 会话创建失败，请稍后重试")
            return
        sid = session["session_id"]
        lock = self._session_locks.setdefault(sid, asyncio.Lock())
        if lock.locked():  # F9：在途 turn/停车中 → 不排队，提示（P0 策略）
            await self.send_markdown(
                staff_id, "Ginno", "⏳ 上一轮仍在进行中（或有问题等你作答），请稍候再发。")
            return
        async with lock:
            tap = DingTalkTap(self, staff_id, conversation_id)
            await run_channel_turn(
                session, text, tap, attached_files=attached or None,
                usage_source=usage_source)

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
