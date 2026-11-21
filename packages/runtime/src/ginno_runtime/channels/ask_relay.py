"""无人值守 ask_user 的企微异步人审（2026-11-21 用户拍板「要做」）。

两条互补路径（main e75c9ca 已把 lastwords auto 改为「弹卡片等人」，
本模块补齐「人不在桌面」时的企微触达）：

**A. 真无人值守 turn（schedule/CLI，``_INTERACTIVE=False``）**：
ask_tools 短路点改为先经 ``relay_ask()``——推企微卡片并**同步阻塞
等待**答复（registry: chat_key 单问题；企微回复数字/文字 → set event
→ 工具返回「用户在企微答复」），超时回退现状「自选默认」回执。
graph 不打断、无需 resume 机制。

**B. 卡片镜像（auto 续跑等有人值守语义、但桌面无人看）**：
``on_turn_event`` 挂在 ``_push_session_event``（与 vibe_island 同型）
——user.question 事件且该 turn 无渠道 tap 时，用 push_only tap 复用
``_send_question``（发卡片 + register pending + 超时 resume）——企微
回复走 wecom adapter 现成的 ``_try_answer`` → resume 管线，近零新
机制。

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


def _relay_enabled() -> bool:
    try:
        raw = json.loads(paths.settings_path().read_text() or "{}")
    except Exception:  # noqa: BLE001 — settings 损坏按未开启
        return False
    return bool(raw.get("ask_relay_wecom"))


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
    """_push_session_event 的全局钩子（与 vibe_island 同型，绝不抛）。"""
    try:
        _note_loop()
        if event != "user.question" or not _relay_enabled() or not session_id:
            return
        asyncio.get_running_loop().create_task(_mirror_question(session_id, data))
    except RuntimeError:
        pass  # 无事件循环（极端场景）——静默


async def _mirror_question(session_id: str, data: dict) -> None:
    ad = _wecom_adapter()
    chat_key = getattr(ad, "last_chat_key", None) if ad else None
    if not ad or not chat_key:
        return
    # 该 turn 已有 wecom tap（企微发起）则不镜像——只有非渠道 turn 才补触达。
    q_id = str(data.get("id") or "")
    if q_id and q_id in getattr(ad, "_own_question_ids", frozenset()):
        return
    from .wecom.tap import WeComTap

    tap = WeComTap.push_only(ad, chat_key)
    payload = dict(data)
    payload.setdefault("session_id", session_id)
    try:
        await tap._send_question(payload)
    except Exception:  # noqa: BLE001 — 镜像失败不影响桌面卡片
        pass
