"""无人值守 ask_user 的企微异步人审（2026-11-21 用户拍板「要做」）。

两条互补路径（main e75c9ca 已把 lastwords auto 改为「弹卡片等人」，
本模块补齐「人不在桌面」时的企微触达）：

**A. 真无人值守 turn（schedule/CLI，``_INTERACTIVE=False``）**：
ask_tools 短路点改为先经 ``relay_ask()``——推企微卡片并**同步阻塞
等待**答复（registry: chat_key 单问题；企微回复数字/文字 → set event
→ 工具返回「用户在企微答复」），超时回退现状「自选默认」回执。
graph 不打断、无需 resume 机制。

**B. 卡片镜像（auto 续跑等有人值守语义、但桌面无人看）**：
两条事件出口都调用 ``on_turn_event``（流式引擎和 ``_push_session_event``）。
无渠道 tap 时主动推送问题。有绑定的企微会话使用自己的聊天地址，桌面会话
使用最近活跃地址。企微回复经 ``_try_answer`` 进入统一的 ``resume_question``。

开关：settings.json ``"ask_relay_wecom": true``（默认关；无 UI）。
fail-safe：wecom 未连接/无活跃 chat_key → A 直接回退现状回执、B 静默
跳过——绝不影响原语义。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ginno_runtime import paths
from ginno_runtime import server_shared as shared


def _relay_enabled() -> bool:
    try:
        raw = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and bool(raw.get("ask_relay_wecom"))


def _wecom_adapter():
    """取已连接的 wecom adapter（connectors/builtin 模块单例）；无 → None。"""
    try:
        from ..connectors import builtin as cb

        ad = cb._wecom_adapter
        return ad if ad and getattr(ad, "client", None) is not None else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# A. 无人值守的同步阻塞等待（每 chat 同时至多一个在等问题）
# ---------------------------------------------------------------------------

_UNATTENDED_Q: dict[str, dict[str, Any]] = {}
# 主事件循环锚点（同步工具跑在 executor 线程，等待要用
# run_coroutine_threadsafe 回主 loop；on_turn_event/adapter 入站时记录）。
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None


def _note_loop() -> None:
    global _MAIN_LOOP
    try:
        _MAIN_LOOP = asyncio.get_running_loop()
    except RuntimeError:
        pass


def relay_ask_sync(
    question: str, options: list[str], header: str, timeout_s: float
) -> str | None:
    """同步工具（executor 线程）用的等待版：跨线程投递回主 loop。"""
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        return None
    fut = asyncio.run_coroutine_threadsafe(
        relay_ask(question, options, header, timeout_s), loop
    )
    try:
        return fut.result(timeout_s + 5)
    except Exception:  # noqa: BLE001 — 任何失败回退自选默认
        return None


def _parse_answer(text: str, options: list[str]) -> str | None:
    """企微回复 → 选项答案：数字序号（1-based）或子串匹配；否则原文。"""
    t = text.strip()
    if not t:
        return None
    if t.isdigit():
        i = int(t) - 1
        if 0 <= i < len(options):
            return options[i]
        return None  # 越界数字不算（防误触）
    for opt in options:
        if t in str(opt):
            return str(opt)
    return t  # 自由文本（allow_free_text 语义）


def try_answer_unattended(chat_key: str, text: str) -> bool:
    """企微入站文本先于新 turn 消化：命中在等的问题 → resolve 并消化。

    返回 True 表示该消息是答复，adapter 不得再 dispatch 新 turn。"""
    slot = _UNATTENDED_Q.get(chat_key)
    if not slot or slot.get("event").is_set():
        return False
    ans = _parse_answer(text, list(slot.get("options") or []))
    if ans is None:
        return False
    slot["answer"] = ans
    slot["event"].set()
    return True


async def relay_ask(
    question: str, options: list[str], header: str, timeout_s: float
) -> str | None:
    """推企微「无人值守提问」卡片并阻塞等待；无渠道/超时 → None。"""
    ad = _wecom_adapter()
    chat_key = getattr(ad, "last_chat_key", None) if ad else None
    if not chat_key:
        return None
    lines = [
        f"\n\n🤖 **{header or '无人值守提问'}**",
        str(question or ""),
    ]
    for i, opt in enumerate(options):
        lines.append(f"{i + 1}. {opt}")
    lines.append(f"\n（无人值守回合提问，回复数字或文字作答；{int(timeout_s)}s 内有效，超时自动选默认）")
    try:
        await ad.client.send_markdown(chat_key, "\n".join(lines))
    except Exception:  # noqa: BLE001 — 推送失败按未开启回退
        return None
    ev = asyncio.Event()
    _UNATTENDED_Q[chat_key] = {
        "event": ev, "answer": None, "options": options, "t0": time.monotonic()
    }
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout_s)
        return _UNATTENDED_Q[chat_key]["answer"]
    except asyncio.TimeoutError:
        try:
            await ad.client.send_markdown(chat_key, "⏱️ 无人值守提问超时，已自动选择默认继续。")
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        _UNATTENDED_Q.pop(chat_key, None)


# ---------------------------------------------------------------------------
# B. user.question 事件镜像（auto 续跑等「弹卡片等人」但桌面无人看）
# ---------------------------------------------------------------------------

def on_turn_event(session_id: str | None, event: str, data: dict) -> None:
    """在后台镜像提问，桌面发帧无需等待企微回执。"""
    _note_loop()
    if event == "user.question" and session_id and _relay_enabled():
        shared.spawn_bg(_mirror_question(session_id, data))


async def _mirror_question(session_id: str, data: dict) -> None:
    from .base import ChannelTap
    from .wecom.tap import WeComTap

    sockets = shared._SESSION_WS.get(session_id) or []
    if any(isinstance(socket, ChannelTap) for socket in sockets):
        return  # 正常渠道回合由自己的 tap 发送。
    channel = (shared._SESSIONS.get(session_id) or {}).get("channel")
    if channel and channel != "wecom":
        return
    if data.get("multi"):
        # 企微文本回执一次只能提交一个答案。
        return

    try:
        ad = _wecom_adapter()
        chat_key = ad.question_destination(session_id) if ad else None
        if not chat_key:
            shared._log.info("wecom_question_mirror_no_destination session=%s", session_id)
            return
        q_id = str(data.get("qid") or data.get("id") or "")
        if q_id and q_id in ad._own_question_ids:
            return
        tap = WeComTap.push_only(ad, chat_key)
        tap.session_id = session_id
        if await tap._send_question(data):
            shared._log.info("wecom_question_mirror_sent session=%s", session_id)
        else:
            shared._log.info("wecom_question_mirror_chat_busy session=%s", session_id)
    except Exception as exc:  # noqa: BLE001 — 镜像失败时桌面卡片仍可作答
        shared._log.warning(
            "wecom_question_mirror_failed session=%s error_type=%s",
            session_id, type(exc).__name__,
        )
