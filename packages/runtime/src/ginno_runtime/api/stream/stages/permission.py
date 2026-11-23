"""permission 阶段 — interrupt 停车：permission_request / version_propose /
user_question（批次 5-2）。

复制自 legacy ``engine._stream_graph`` 的 ``__interrupt__`` 分支
（legacy engine.py:1119-1152）。ask_user 预算（begin_ask_budget）在编排器
入口武装（run.py，对应 legacy 函数第一行）；permission_response 的恢复由
ws 层走 ``_run_resume`` → ``Command(resume=...)`` 再入本引擎，与 legacy
同一入口。

**注册表写入点③**：``_PENDING_RESUME.add`` / ``_PENDING_KIND[...] = ...``
三种 interrupt 各一处，与 legacy 同行序（saw_interrupt 置位 → 登记 →
log → 事件帧）。
"""

from __future__ import annotations

from .. import engine as eng
from .ctx import StreamTurnCtx


async def handle_interrupt(ctx: StreamTurnCtx, delta) -> None:
    """legacy engine.py:1119-1152 的 ``__interrupt__`` 分支。"""
    items = delta if isinstance(delta, (list, tuple)) else [delta]
    for intr in items:
        value = getattr(intr, "value", None) or intr
        if (
            isinstance(value, dict)
            and value.get("kind") == "permission_request"
        ):
            ctx.saw_interrupt = True
            eng._PENDING_RESUME.add(ctx.session_id)
            eng._PENDING_KIND[ctx.session_id] = "permission_request"
            eng._log.info(
                "turn_interrupt session=%s turn=%s kind=%s",
                ctx.session_id, ctx.turn_id, value.get("kind"),
            )
            await ctx.safe_send(
                ctx.emit(
                    "permission.request",
                    {
                        "tool": value.get("tool"),
                        "args": value.get("args"),
                    },
                )
            )
        elif isinstance(value, dict) and value.get("kind") == "version_propose":
            # P5: workflow edit awaiting diff confirmation.
            # Independent of the permission system; resumed via
            # the same permission_response WS message.
            ctx.saw_interrupt = True
            eng._PENDING_RESUME.add(ctx.session_id)
            eng._PENDING_KIND[ctx.session_id] = "version_propose"
            eng._log.info(
                "turn_interrupt session=%s turn=%s kind=%s",
                ctx.session_id, ctx.turn_id, value.get("kind"),
            )
            await ctx.safe_send(
                ctx.emit(
                    "version.propose",
                    {
                        "workflow_id": value.get("workflow_id"),
                        "from_version": value.get("from_version"),
                        "diff": value.get("diff", ""),
                        "rationale": value.get("rationale", ""),
                    },
                )
            )
        elif isinstance(value, dict) and value.get("kind") == "user_question":
            # ask_user parked the turn on an ambiguity. The
            # card is a transcript block (not a ref like the
            # permission prompt), so this event carries the
            # tool_call id the client merges on.
            ctx.saw_interrupt = True
            eng._PENDING_RESUME.add(ctx.session_id)
            eng._PENDING_KIND[ctx.session_id] = "user_question"
            eng._log.info(
                "turn_interrupt session=%s turn=%s kind=%s set_id=%d",
                ctx.session_id, ctx.turn_id, value.get("kind"),
                id(eng._PENDING_RESUME),
            )
            # Worker ask_user 中断透传（relay-question 设计）：engine 层
            # 登记钩子（不依赖 ws——headless wake turn 的 parked 同样过
            # 这里）。无订阅者时 note_parked_question 直接返回 None，
            # 既有行为零变化；qid 凭证由 ask_tools 在 interrupt payload
            # 里生成并随 checkpoint 落盘（三层凭证链的第一层）。
            try:
                from ....worker_bus.question_sync import sync_parked_answer
                from ....worker_bus.relay import note_parked_question

                if await sync_parked_answer(ctx.session_id, value):
                    continue
                await note_parked_question(ctx.session_id, ctx.turn_id, value)
            except Exception:
                eng._log.exception(
                    "relay_parked_hook_failed session=%s turn=%s",
                    ctx.session_id, ctx.turn_id,
                )
            await ctx.safe_send(
                ctx.emit(
                    "user.question",
                    {
                        "id": value.get("id"),
                        "qid": value.get("qid"),
                        "question": value.get("question", ""),
                        "header": value.get("header", ""),
                        "options": value.get("options") or [],
                        "allow_free_text": value.get("allow_free_text", True),
                        # 多问题（契约 v2，ask_tools questions 参数）：
                        # 逐题字段原样透传，前端一张卡渲染全部子题。
                        "multi": bool(value.get("multi")),
                        "questions": value.get("questions") or [],
                    },
                )
            )
