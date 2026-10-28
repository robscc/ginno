"""DingTalkTap：把引擎 turn 帧翻译成钉钉侧动作（设计 §5 分发表 + F3/F4/F7）。

帧契约（``server_shared._ev``）：``{"event": name, "turn_id"?: ..., **data}``。
本类只做翻译与节流，不做业务决策；会话驱动在 ``base.run_channel_turn``，
恢复入口在 ``api/stream/resume.py``。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ..base import ChannelTap

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .adapter import DingTalkChannelAdapter

# F3 节流参数（§7 待验证项 3：接口频率上限实测后校准）
_FLUSH_INTERVAL_S = 0.5
_FLUSH_MIN_CHARS = 400

# F7 产物上行：可直接发送的扩展名（其余类型发文件 + 预览说明）
_PREVIEWABLE = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "docx",
                "xlsx", "pptx", "txt", "csv", "md"}
_SENDABLE = _PREVIEWABLE | {"html", "zip", "json", "tsv", "log", "py", "js", "ts", "go", "rs"}
# 图片类走 picture 消息（点开大图），其余走 file 消息
_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

_STATUS_ICON = {"done": "✅", "running": "⏳", "pending": "🕒", "ok": "✅", "error": "❌"}


def _widget_to_markdown(data: dict) -> str:
    """widget.emit 的渠道降级渲染（设计 §6 ⚠️：钉钉无等价动态组件）。

    stat_list → 列表；chart → 表格 + 数据来源行。只翻译 data 里真实存在的
    数字，绝不发明数据。
    """
    kind = data.get("kind")
    payload = data.get("data") or {}
    lines: list[str] = []
    if kind == "stat_list":
        if payload.get("title"):
            lines.append(f"**{payload['title']}**")
        for it in payload.get("items") or []:
            icon = _STATUS_ICON.get(str(it.get("status", "")), "•")
            lines.append(f"- {icon} {it.get('label', '')}: {it.get('value', '')}")
    elif kind == "chart":
        if payload.get("title"):
            lines.append(f"**{payload['title']}**")
        rows = payload.get("data") or []
        xk, yk = payload.get("x", "x"), payload.get("y", "y")
        if rows:
            lines.append(f"| {xk} | {yk} |")
            lines.append("|---|---|")
            for r in rows[:30]:
                lines.append(f"| {r.get(xk, '')} | {r.get(yk, '')} |")
        if payload.get("summary"):
            lines.append(str(payload["summary"]))
    else:
        return ""
    return "\n".join(lines)


class DingTalkTap(ChannelTap):
    """一个 turn 一枚 tap：挂 ``_SESSION_WS`` 收帧，驱动钉钉卡片/消息。

    降级链（F3）：卡片创建失败 → ``_card_failed``，正文改走 markdown 消息，
    终态再发一次全量；流式更新失败 → 静默重试，终态保证可见。
    """

    def __init__(self, adapter: DingTalkChannelAdapter, staff_id: str,
                 conversation_id: str) -> None:
        self.adapter = adapter
        self.staff_id = staff_id
        self.conversation_id = conversation_id
        self._buf: list[str] = []
        self._buf_len = 0
        self._last_flush = 0.0
        self._full_md = ""          # 全量正文（终态/降级兜底）
        self._card: Any = None      # cards.StreamCard
        self._card_failed = False
        self._finalized = False
        self._ask_timer: asyncio.TimerHandle | None = None
        self._question_id: str | None = None

    # ---- F3 流式输出 -------------------------------------------------------

    async def _create_card(self) -> None:
        try:
            self._card = await self.adapter.cards.create_stream_card(
                self.staff_id, placeholder="⏳ 思考中…")
        except Exception:
            self._card_failed = True  # 降级：普通 markdown 消息

    async def _maybe_flush(self, *, force: bool = False) -> None:
        if not self._buf:
            return
        now = time.monotonic()
        due = force or (now - self._last_flush >= _FLUSH_INTERVAL_S
                        and self._buf_len > 0) or self._buf_len >= _FLUSH_MIN_CHARS
        if not due:
            return
        chunk = "".join(self._buf)
        self._full_md += chunk
        self._buf.clear()
        self._buf_len = 0
        self._last_flush = now
        if self._card_failed or self._card is None:
            return  # 降级模式不流式，终态一次性发
        try:
            await self.adapter.cards.stream_update(self._card, self._full_md)
        except Exception:
            pass  # 单批失败静默；终态 finalize 兜底全量

    async def _finalize(self, note: str = "") -> None:
        """终态：flush 残余 + 卡片定格（或降级全量消息）。幂等。"""
        if self._finalized:
            return
        self._finalized = True
        await self._maybe_flush(force=True)
        body = self._full_md or "（无输出）"
        if note:
            body = f"{body}\n\n---\n{note}"
        if self._card is not None:
            try:
                await self.adapter.cards.finalize(self._card, body)
                return
            except Exception:
                pass
        # 降级 / 卡片终态失败：一条 markdown 消息保证可见
        try:
            await self.adapter.send_markdown(self.staff_id, "Ginno", body)
        except Exception:
            pass

    # ---- F4 ask_user -------------------------------------------------------

    async def _send_question(self, data: dict) -> None:
        q = {
            "id": data.get("id"),
            "question": data.get("question", ""),
            "header": data.get("header", ""),
            "options": data.get("options") or [],
            "allow_free_text": data.get("allow_free_text", True),
        }
        self._question_id = str(q["id"] or "")
        # register_question 是协程（内部持 _q_lock）：漏 await 时卡片回调
        # 查不到登记、静默 no-op——提问永远无法作答（wecom 侧同路径有 await）。
        await self.adapter.register_question(self._question_id, self.session_id)
        try:
            await self.adapter.cards.send_question_card(self.staff_id, q)
        except Exception:
            # 问题卡片失败也不能让问题无门可答：降级发文本 + 「回复编号」语义
            opts = "\n".join(f"{i+1}. {o}" for i, o in enumerate(q["options"]))
            await self.adapter.send_markdown(
                self.staff_id, q["header"] or "Ginno 提问",
                f"{q['question']}\n\n{opts}\n\n（直接回复数字或文字作答；回复「跳过」跳过）",
            )
        # F4 步骤 7：超时自动 skip
        timeout_s = float(self.adapter.config.get("ask_timeout_s") or 600)
        loop = asyncio.get_running_loop()
        self._ask_timer = loop.call_later(
            timeout_s, lambda: asyncio.create_task(self._ask_timeout()))

    async def _ask_timeout(self) -> None:
        from ...api.stream.resume import resume_pending_interrupt

        if self._question_id:
            await self.adapter.unregister_question(self._question_id)
            try:
                await self.adapter.cards.disable_question_card(self._question_id)
            except Exception:
                pass
        resume_pending_interrupt(
            self.session_id,
            {"kind": "user_answer", "answer": "", "option_index": None, "skip": True},
        )

    def _cancel_ask_timer(self) -> None:
        if self._ask_timer is not None:
            self._ask_timer.cancel()
            self._ask_timer = None
        if self._question_id:
            # 同步上下文里退登记：ensure_future 排进当前 loop（与 wecom
            # _cancel_ask_timer 同款）；done-callback 吃掉异常避免未观测告警。
            task = asyncio.ensure_future(
                self.adapter.unregister_question(self._question_id))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # ---- F7 产物上行 -------------------------------------------------------

    async def _send_artifact(self, path_str: str, name: str = "") -> None:
        from pathlib import Path

        p = Path(path_str).expanduser()
        if not p.is_file():
            return
        ext = p.suffix.lstrip(".").lower()
        if ext not in _SENDABLE:
            return  # 未知类型不外发（安全默认）
        try:
            mtype = "image" if ext in _IMAGE_EXT else "file"
            media_id = await self.adapter.api.upload_media(p, type=mtype)
            await self.adapter.send_media(
                self.staff_id, media_id, file_name=name or p.name, file_ext=ext,
                is_image=ext in _IMAGE_EXT)
        except Exception:
            # 上传失败降级为路径回执（F7 异常分支）
            await self.adapter.send_markdown(
                self.staff_id, "Ginno", f"📎 产物文件（钉钉上传失败，见桌面端）: `{p}`")

    # ---- 分发表（§5）--------------------------------------------------------

    async def on_frame(self, event: str, data: dict) -> None:
        if event == "turn.start":
            await self._create_card()
        elif event == "token.delta":
            txt = str(data.get("content") or "")
            if txt:
                self._buf.append(txt)
                self._buf_len += len(txt)
                await self._maybe_flush()
        elif event == "message.end":
            await self._maybe_flush(force=True)
        elif event == "user.question":
            await self._send_question(data)
        elif event == "permission.request":
            # P0（设计 §6 ❌/F5）：高危确认钉钉侧自动拒绝并说明；P1 改卡片确认。
            from ...api.stream.resume import resume_pending_interrupt

            resume_pending_interrupt(self.session_id, {"decision": "deny"})
            tool = str(data.get("tool") or data.get("name") or "tool")
            await self.adapter.send_markdown(
                self.staff_id, "Ginno",
                f"⚠️ 高危操作 `{tool}` 在钉钉渠道默认拒绝（P0 策略）。请到桌面端 Ginno 执行。",
            )
        elif event == "image.emit":
            from ... import files as files_mod

            entry = files_mod.get_by_id(str(data.get("file_id") or ""))
            if entry is not None:
                await self._send_artifact(str(entry.path), str(data.get("name") or entry.name))
        elif event == "ref.emit":
            if str(data.get("kind") or "file") in ("file", "doc", "link"):
                await self._send_artifact(str(data.get("ref_id") or ""),
                                          str(data.get("name") or ""))
        elif event == "widget.emit":
            md = _widget_to_markdown(data.get("data") or {})
            if md:
                self._buf.append(f"\n\n{md}\n")
                self._buf_len += len(md) + 4
        elif event == "turn.stopped":
            self._cancel_ask_timer()
            await self._finalize("⏹ 已停止")
        # thinking.delta / tool.* / keepalive / artifacts.changed / …: P0 忽略

    async def close(self) -> None:
        """turn 结束兜底：cancel 定时器 + 未终态时补一次 finalize。"""
        self._cancel_ask_timer()
        await self._finalize()
