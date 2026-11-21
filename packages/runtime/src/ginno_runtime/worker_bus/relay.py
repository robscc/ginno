"""Worker ask_user 中断透传（relay-question 设计，2026-11-21）。

Worker（被订阅的会话）parked 在 ask_user 上时，问题不只挂在自己的
WS 连接上——订阅它的会话（订阅方）应当能代为转达：用自己的 ask_user
问自己的用户，把回答映射回原选项后 relay 回来 resume worker。

三层 qid 凭证链：

1. **生成**：``ask_tools.ask_user`` 构建 interrupt payload 时生成
   ``qid = "q_" + uuid4().hex[:12]`` —— payload 随 checkpoint 落盘，
   重启后 ``get_pending_interrupt`` 重读自然带出（这就是「入 parked
   上下文」：qid 的持久化载体就是中断本身，不另建副本）。
2. **登记**：engine 层 ``stages/permission.handle_interrupt`` 的
   user_question 分支调 :func:`note_parked_question`（本模块）——
   无订阅者直接返回（零回归），有订阅者才进 ``_PENDING_RELAY`` 并向
   订阅方注入编排指令（③ 模板）。
3. **即销**：任何路径 resume 了该问题（用户直答 / steer 即答 /
   relay_answer 代答）→ :func:`mark_answered` 即销登记条目、写墓碑
   （answered_by / 答案摘要 / answered_at，TTL 24h）——之后的
   relay_answer 命中墓碑返回 ``already_answered``（纯只读四不：
   不 resume / 不写 checkpoint / 不广播 / 不动注册表）。

幂等零影响（v5）：墓碑只服务「晚到的 relay 答案」一个消费者，读路径
绝不触碰 worker 侧任何状态。
"""

from __future__ import annotations

import json as _json
import os
import secrets
import time
import uuid
from typing import Any

from .. import paths
from ..lang import t
from ..server_shared import _log, _push_session_event
from ..session_meta import _find_meta

# qid -> 登记条目（内存真源；落盘副本供重启恢复）。
_PENDING_RELAY: dict[str, dict] = {}
# qid -> 墓碑 {answered_by, answer, answered_at}。
_RELAY_TOMBSTONES: dict[str, dict] = {}
_TOMBSTONE_TTL_S = 24 * 3600

_STATE_FILENAME = "relay-state.json"


def new_qid() -> str:
    """qid 凭证格式（与方案 v3 一致）：``q_`` + uuid4 hex 前 12 位。"""
    return "q_" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- #
# 落盘 / 恢复
# --------------------------------------------------------------------------- #


def _state_path():
    return paths.home() / _STATE_FILENAME


def _atomic_write_text(p, text: str) -> None:
    """原子写（复用仓库范式，同 ``context_folders._atomic_write``）：
    同目录唯一随机后缀 tmp + ``os.replace``——replace 在同一文件系统上
    原子完成，崩溃半写只会留在 tmp（可清理），真文件要么是旧完整内容、
    要么是新完整内容，绝不出现截断态。tmp 写失败/replace 失败时删 tmp
    再抛，不留垃圾文件。"""
    tmp = p.with_name(f".{p.name}.tmp{secrets.token_hex(3)}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _persist_unlocked() -> None:
    """内存表 → 磁盘（登记/即销时调用；失败静默——恢复降级为「无 pending」，
    worker 侧 parked 事实仍在 checkpoint，不丢答案只丢代答便利）。

    原子写：失败发生在 replace 前（tmp 写坏 / replace 被打断）时，真文件
    保持上一次成功写入的完整旧内容——重启恢复不丢已落盘的 pending /
    墓碑；失败原因记 ``relay_state_persist_failed`` 日志。"""
    try:
        now = int(time.time())
        # 顺带做 TTL 惰性清理：墓碑过 24h 即可丢（晚到答案的幂等窗口
        # 远小于 24h）。
        for qid in [q for q, tb in _RELAY_TOMBSTONES.items()
                    if now - int(tb.get("answered_at") or 0) > _TOMBSTONE_TTL_S]:
            _RELAY_TOMBSTONES.pop(qid, None)
        _atomic_write_text(
            _state_path(),
            _json.dumps(
                {"version": 1,
                 "pending": _PENDING_RELAY,
                 "tombstones": _RELAY_TOMBSTONES},
                ensure_ascii=False,
            ),
        )
    except Exception:
        _log.exception("relay_state_persist_failed")


def load_state() -> None:
    """启动恢复（幂等）：读盘重建 pending 表 + 墓碑。条目里 worker 是否
    仍 parked 不在此校验（要开 checkpointer，重）——留给 relay_answer
    调用时由 ``resume_question`` 的守卫/checkpointer 双信号真实校验。"""
    try:
        p = _state_path()
        if not p.exists():
            return
        data = _json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        for qid, ent in (data.get("pending") or {}).items():
            if isinstance(ent, dict):
                _PENDING_RELAY[str(qid)] = ent
        for qid, tb in (data.get("tombstones") or {}).items():
            if isinstance(tb, dict):
                _RELAY_TOMBSTONES[str(qid)] = tb
        _log.info("relay_state_loaded pending=%d tombstones=%d",
                  len(_PENDING_RELAY), len(_RELAY_TOMBSTONES))
    except Exception:
        _log.exception("relay_state_load_failed")


def _reset_state() -> None:
    """测试用：清空内存表（不删盘）。"""
    _PENDING_RELAY.clear()
    _RELAY_TOMBSTONES.clear()


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def find_pending(qid: str) -> dict | None:
    return _PENDING_RELAY.get(str(qid or "")) or None


def tombstone_of(qid: str) -> dict | None:
    return _RELAY_TOMBSTONES.get(str(qid or "")) or None


def relay_pending_for(subscriber_id: str) -> list[dict]:
    """该订阅者当前可代答的登记条目（多 worker / 同 worker 多问都在列表）。"""
    out = []
    for ent in _PENDING_RELAY.values():
        if subscriber_id in (ent.get("subscribers") or []):
            out.append(ent)
    out.sort(key=lambda e: int(e.get("created_at") or 0))
    return out


def _pending_questions_payload(subscriber_id: str) -> list[dict]:
    """digest/事件侧的 pending_questions 列表（goal ② 字段契约）。"""
    rows = []
    for ent in relay_pending_for(subscriber_id):
        payload = ent.get("payload") or {}
        rows.append(
            {
                "qid": ent.get("qid"),
                "worker_sid": ent.get("worker_sid"),
                "turn_id": ent.get("turn_id"),
                "header": str(payload.get("header") or ""),
                "options": list(payload.get("options") or []),
                "multi": bool(payload.get("multi")),
                "prefill": str(payload.get("prefill") or ""),
                "worker_title": str(ent.get("worker_title") or ""),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# 登记（①）+ 注入（②③）
# --------------------------------------------------------------------------- #


def _worker_subscribers(worker_sid: str, meta: dict) -> list[str]:
    """meta.subscribers 里的订阅者（去重、排除 worker 自身）。terminal 与
    digest 订阅者都要——订阅者在等 worker 产出，worker 卡问题上时它就是
    代答候选人。"""
    out: list[str] = []
    for s in meta.get("subscribers") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("session_id") or "")
        if sid and sid != worker_sid and sid not in out:
            out.append(sid)
    return out


def _relay_instructions(entry: dict, *, reminder: bool = False) -> str:
    """注入编排指令（方案 v3 模板）。``reminder``：已问未答重注入时标注。"""
    payload = entry.get("payload") or {}
    worker_sid = str(entry.get("worker_sid") or "")
    title = str(entry.get("worker_title") or worker_sid[:8])
    qid = str(entry.get("qid") or "")
    multi = bool(payload.get("multi"))
    if multi:
        subs = payload.get("questions") or []
        q_lines = "\n".join(
            f"- 子问题 {i + 1}：{str(s.get('question') or '')}"
            f"（选项：{' / '.join(str(o) for o in (s.get('options') or []))}）"
            for i, s in enumerate(subs)
            if isinstance(s, dict)
        )
        q_block = f"问题（多问题卡，逐题作答）：\n{q_lines}"
        choice_hint = (
            'choice 传数组（按子问题顺序逐题给原选项的完整文本），例如 '
            f'relay_answer(worker_session="{worker_sid}", qid="{qid}", '
            'choice=["<子问题1的原选项>", "<子问题2的原选项>"], mapping_reason="…")'
        )
    else:
        q = str(payload.get("question") or "")
        opts = [str(o) for o in (payload.get("options") or [])]
        q_block = f"问题：{q}\n选项：\n" + "\n".join(f"- {o}" for o in opts)
        choice_hint = (
            f'relay_answer(worker_session="{worker_sid}", qid="{qid}", '
            'choice="<上面原选项之一的完整文本>", mapping_reason="…")'
        )
    head = t(
        f'[waiting] Worker "{title}" is parked on a question that needs a '
        "user decision. Relay it to YOUR user:",
        f'[waiting] Worker「{title}」停在一个需要用户决策的问题上，请你代为转达：',
    ) if not reminder else t(
        f'[reminder: still unanswered] Worker "{title}" asked you to relay '
        "this question earlier; the user has not answered yet:",
        f'[提醒：此问题此前已问过你、用户尚未回答] Worker「{title}」等待转达的问题：',
    )
    return (
        f'<ginno_worker_question worker="{worker_sid}" qid="{qid}">\n'
        f"{head}\n{q_block}\n\n"
        + t(
            "How to relay:\n"
            "1. Ask YOUR user with the ask_user tool. You may rephrase the "
            "question, but the options must keep the SAME meaning and the "
            "SAME count as listed above.\n"
            "2. When you have 3 or fewer pending questions to relay, prefer "
            "ONE ask_user(questions=[...]) card asking them together.\n"
            "3. After the user answers, map their answer back to one of the "
            "original options above, WRITE DOWN your mapping reason first, "
            "then call relay_answer with it.\n",
            "转达方式：\n"
            "1. 用 ask_user 工具问你的用户。可以改述问题措辞，但每个选项的"
            "含义与数量必须与上面一致。\n"
            "2. 待转达问题 ≤3 个时，建议一次 ask_user(questions=[…]) 一屏问完。\n"
            "3. 用户回答后，把答案映射回上面的原选项，先写下映射理由，再调用 "
            "relay_answer。\n",
        )
        + f"4. {choice_hint}\n</ginno_worker_question>"
    )


async def note_parked_question(
    worker_sid: str, turn_id: str, value: dict
) -> str | None:
    """engine 层 user_question parked 钩子（permission.handle_interrupt 调）。

    无订阅者 → 返回 None（不登记、不注入——既有行为零变化）。有订阅者
    → 登记 + 向每个订阅者注入编排指令（② digest 事件 + ③ 注入模板）。
    """
    if not isinstance(value, dict) or value.get("kind") != "user_question":
        return None
    found = _find_meta(worker_sid)
    if not found:
        return None
    meta, _slug = found
    subs = _worker_subscribers(worker_sid, meta)
    if not subs:
        return None
    qid = str(value.get("qid") or "") or new_qid()
    payload = {
        "question": str(value.get("question") or ""),
        "options": [str(o) for o in (value.get("options") or [])],
        "header": str(value.get("header") or ""),
        "allow_free_text": bool(value.get("allow_free_text", True)),
        "multi": bool(value.get("multi")),
        "questions": [
            {k: s.get(k) for k in ("question", "options", "header",
                                   "allow_free_text")}
            for s in (value.get("questions") or []) if isinstance(s, dict)
        ],
        "prefill": "",
    }
    now = int(time.time())
    entry = {
        "qid": qid,
        "worker_sid": worker_sid,
        "turn_id": str(turn_id or ""),
        "worker_title": str(meta.get("title") or worker_sid[:8]),
        "created_at": now,
        "payload": payload,
        "subscribers": subs,
        "asked": {},
    }
    _PENDING_RELAY[qid] = entry
    _persist_unlocked()
    _log.info("relay_parked worker=%s qid=%s subs=%d", worker_sid, qid, len(subs))
    await _push_to_subscribers(entry, reminder=False)
    return qid


async def _push_to_subscribers(entry: dict, *, reminder: bool) -> None:
    """向登记条目的订阅者注入编排指令 + pending_questions digest 事件。

    注入复用 worker_bus 既有通道（``_inject_result``：忙 → steer 工具
    边界吸收；闲 → coalesce wake 新 turn）——订阅方收到的是一条普通
    注入消息，信封即模板。"""
    from ..subagent_scheduler import _inject_result

    worker_sid = str(entry.get("worker_sid") or "")
    qid = str(entry.get("qid") or "")
    now = int(time.time())
    text = _relay_instructions(entry, reminder=reminder)
    for sid in entry.get("subscribers") or []:
        # ② digest 增强：pending_questions 列表随事件下发（前端未识别
        # 该事件名时静默忽略——零前端改动的容错语义）。
        try:
            await _push_session_event(
                sid,
                "worker.pending_questions",
                {"questions": _pending_questions_payload(sid)},
                "",
            )
        except Exception:
            pass
        try:
            await _inject_result(
                sid, text, worker_sid,
                extra_kwargs={"ginno_worker_msg": {
                    "from": worker_sid, "kind": "question", "qid": qid,
                    "sent_at": now,
                }},
            )
            entry.setdefault("asked", {})[sid] = now
        except Exception:
            _log.exception("relay_push_failed worker=%s sub=%s qid=%s",
                           worker_sid, sid, qid)


# --------------------------------------------------------------------------- #
# 即销（墓碑）
# --------------------------------------------------------------------------- #


def mark_answered(qid: str, answered_by: str, answer: str) -> None:
    """登记条目 → 墓碑（TTL 24h）。answered_by：直答者（"user" 或订阅方
    session id）；answer：答案摘要（选项文本；multi 为逐题摘要）。"""
    qid = str(qid or "")
    ent = _PENDING_RELAY.pop(qid, None)
    if ent is None:
        return
    _RELAY_TOMBSTONES[qid] = {
        "answered_by": str(answered_by or ""),
        "answer": str(answer or "")[:400],
        "answered_at": int(time.time()),
        "worker_sid": str(ent.get("worker_sid") or ""),
    }
    _persist_unlocked()
    _log.info("relay_answered qid=%s by=%s", qid, answered_by)


def note_session_resumed(session_id: str, answered_by: str, answer: str) -> None:
    """resume_question 共享段的收口钩子：该 worker 的待答条目若有，即销。

    用户直答 / steer 即答 / relay_answer 三路 resume 都过这里——保证
    「晚到的另一路 relay」必然命中墓碑而非僵尸 pending 条目。"""
    for qid in [q for q, e in _PENDING_RELAY.items()
                if e.get("worker_sid") == session_id]:
        mark_answered(qid, answered_by, answer)


# --------------------------------------------------------------------------- #
# relay_answer 工具（④）+ 条件注入（⑥）
# --------------------------------------------------------------------------- #


def _already_answered_shape(tb: dict) -> dict:
    return {
        "ok": False,
        "already_answered": True,
        "answered_by": tb.get("answered_by"),
        "answer": tb.get("answer"),
        "answered_at": tb.get("answered_at"),
        "note": t(
            "This question was already answered; nothing was resumed or "
            "changed.",
            "该问题已被回答；本次调用纯只读，未 resume、未改任何状态。",
        ),
    }


def _map_choice(entry: dict, choice: Any) -> tuple[dict | None, str]:
    """choice → user_answer resume value。硬校验：必须在原选项集。

    返回 (resume_value, error)：error 非空时 value 为 None（不 resume，
    错误信息列合法选项触发 LLM 自纠）。
    """
    payload = entry.get("payload") or {}
    if payload.get("multi"):
        subs = [s for s in (payload.get("questions") or []) if isinstance(s, dict)]
        if not isinstance(choice, list):
            return None, t(
                "multi-question card: choice must be a LIST with one entry "
                "per sub-question",
                "多问题卡：choice 必须是数组，逐题一项",
            )
        if len(choice) != len(subs):
            return None, t(
                f"choice has {len(choice)} entries but the card has "
                f"{len(subs)} questions",
                f"choice 有 {len(choice)} 项，但问题卡有 {len(subs)} 题",
            )
        answers = []
        for i, (c, s) in enumerate(zip(choice, subs)):
            opts = [str(o) for o in (s.get("options") or [])]
            if not opts:
                answers.append({"answer": str(c), "option_index": None,
                                "skip": False})
                continue
            cs = str(c)
            if cs not in opts:
                return None, t(
                    f"sub-question {i + 1}: choice {cs!r} is not one of the "
                    f"original options {opts}",
                    f"子问题 {i + 1}：choice {cs!r} 不在原选项集 {opts} 中",
                )
            answers.append({"answer": cs, "option_index": opts.index(cs),
                            "skip": False})
        return {"kind": "user_answer", "multi": True, "answers": answers}, ""
    opts = [str(o) for o in (payload.get("options") or [])]
    cs = str(choice)
    if not opts:
        # 纯自由文本问题：任意回答合法。
        return ({"kind": "user_answer", "answer": cs, "option_index": None,
                 "skip": False}, "")
    if cs not in opts:
        return None, t(
            f"choice {cs!r} is not one of the original options {opts}; pick "
            "one of them (map the user's answer back to the ORIGINAL option "
            "text)",
            f"choice {cs!r} 不在原选项集 {opts} 中；请映射回原选项的完整文本",
        )
    return ({"kind": "user_answer", "answer": cs, "option_index": opts.index(cs),
             "skip": False}, "")


async def relay_answer_impl(
    subscriber_id: str,
    worker_session: str,
    qid: str,
    choice: Any,
    mapping_reason: str = "",
) -> str:
    """relay_answer 工具实现。幂等分支（qid 不在当前 parked 集）纯只读
    四不：不 resume / 不写 checkpoint / 不广播 / 不动注册表。"""
    from ..api.stream.resume import resume_question

    qid = str(qid or "")
    entry = find_pending(qid)
    if entry is None:
        tb = tombstone_of(qid)
        if tb is not None:
            # 纯只读四不——直接返回墓碑 shape。
            return _json.dumps(_already_answered_shape(tb), ensure_ascii=False)
        return _json.dumps(
            {"ok": False,
             "error": t(f"unknown qid {qid!r}",
                        f"未知的 qid {qid!r}"),
             "note": t("no pending question matches this qid (it may have "
                       "been answered long ago and its tombstone expired)",
                       "没有待答问题匹配该 qid（可能很久以前已答、墓碑已过期）")},
            ensure_ascii=False,
        )
    if str(entry.get("worker_sid") or "") != str(worker_session or ""):
        return _json.dumps(
            {"ok": False,
             "error": t(
                 f"qid {qid!r} belongs to worker session "
                 f"{entry.get('worker_sid')!r}, not {worker_session!r}",
                 f"qid {qid!r} 属于 worker 会话 {entry.get('worker_sid')!r}，"
                 f"不是 {worker_session!r}"),
             "note": t("use the worker_session from the pending_questions "
                       "envelope",
                       "请使用 pending_questions 信封里的 worker_session")},
            ensure_ascii=False,
        )
    if not str(mapping_reason or "").strip():
        return _json.dumps(
            {"ok": False,
             "error": t("mapping_reason is required: write WHY the user's "
                        "answer maps to that original option before "
                        "relaying",
                        "mapping_reason 必填：转达前先写明用户的回答为何映射"
                        "到该原选项")},
            ensure_ascii=False,
        )
    value, err = _map_choice(entry, choice)
    if err:
        return _json.dumps({"ok": False, "error": err}, ensure_ascii=False)
    ok = await resume_question(
        entry["worker_sid"],
        answer=str(value.get("answer") or ""),
        option_index=value.get("option_index"),
        skip=bool(value.get("skip", False)),
        multi=bool(value.get("multi")),
        answers=value.get("answers"),
        base_config=None,
        answered_by=subscriber_id,
        relayed_by=subscriber_id,
    )
    if not ok:
        return _json.dumps(
            {"ok": False,
             "error": t("the worker turn could not be resumed (it may have "
                        "just been answered directly by its own user)",
                        "worker 回合未能恢复（可能刚被它自己的用户直答）"),
             "qid": qid},
            ensure_ascii=False,
        )
    # resume_question 成功路径内已 mark_answered（统一收口）；这里补答案
    # 溯源——订阅方 id 即代答人。
    tb = tombstone_of(qid)
    if tb is not None:
        tb["relayed_by"] = subscriber_id
    return _json.dumps(
        {"ok": True, "resumed": True, "qid": qid,
         "worker_session": entry.get("worker_sid"),
         "answer": tb.get("answer") if tb else "",
         "relayed_by": subscriber_id},
        ensure_ascii=False,
    )


def build_relay_tool(subscriber_id: str | None):
    """⑥ 条件注入：``pending_relay[subscriber_id]`` 非空才产出工具。

    agent_node 每次 LLM 请求前重建（qid enum = 当前待答），工具集的
    静态构建与问题的运行时出现由此解耦。"""
    if not subscriber_id:
        return None
    pend = relay_pending_for(subscriber_id)
    if not pend:
        return None
    from langchain_core.tools import tool

    qids = [str(e.get("qid") or "") for e in pend]
    asked_note = ""
    for e in pend:
        if subscriber_id in (e.get("asked") or {}):
            asked_note = t(
                " (NOTE: you already relayed some of these — if the user "
                "answered, relay the answer now; if not, gently remind them)",
                "（提醒：其中有的问题你已问过——用户答了就立即转达，没答可温和提醒）",
            )
            break

    async def relay_answer(
        worker_session: str,
        qid: str,
        choice: str | list[str],
        mapping_reason: str = "",
    ) -> str:
        """Relay YOUR user's answer back to a parked worker question.

        Call this after your ask_user round: map the user's answer back to
        ONE of the worker's ORIGINAL options (exact original text), write
        the mapping reason, then call this tool. Idempotent: a question
        already answered returns already_answered without changing anything.
        """
        return await relay_answer_impl(
            subscriber_id, worker_session, qid, choice, mapping_reason
        )

    relay_answer.__doc__ = (
        "Relay YOUR user's answer back to a parked worker question.\n\n"
        "Call this after your ask_user round: map the user's answer back to "
        "ONE of the worker's ORIGINAL options (exact original text; for a "
        "multi-question card pass a LIST with one original option per "
        "sub-question), write the mapping reason first, then call this "
        "tool. Idempotent: a question that was already answered returns "
        "already_answered without resuming or changing anything."
        + asked_note
        + f"\n\nqid must be one of: {', '.join(qids)}"
    )

    # qid 硬提示：schema 层 enum=当前待答（服务端 parked 校验仍在
    # relay_answer_impl——三层凭证链的第二、三层）。
    import pydantic

    class _RelayArgs(pydantic.BaseModel):
        worker_session: str = pydantic.Field(
            description="worker session id (from the ginno_worker_question envelope)"
        )
        qid: str = pydantic.Field(
            description=f"question credential; one of: {', '.join(qids)}",
        )
        choice: str | list[str] = pydantic.Field(
            description="the ORIGINAL option text (list, one per sub-question,"
                        " for multi-question cards)",
        )
        mapping_reason: str = pydantic.Field(
            default="",
            description="why the user's answer maps to that original option",
        )

    return tool(relay_answer, args_schema=_RelayArgs)


def build_relay_tool_loose(subscriber_id: str | None):
    """tools_node 执行侧兜底（relay-question ⑥ 的执行对称补全）。

    ``build_relay_tool`` 只在 pending 登记非空时产出（bind 侧语义正确：
    没有待答问题时模型不该看到这个工具）。但模型**已经发出**的
    relay_answer 调用必须可执行——bind 发生在 superstep 前，执行点登记
    可能已被即销（worker 用户直答的竞态窗口，e2e 实录：默认 ask 权限下
    订阅方的批准等待正好落在这个窗口里）。此变体不设 qid enum，凭证
    校验全交给服务端 ``relay_answer_impl``（unknown qid / already_answered
    墓碑都是合法 JSON 回执，模型可据此收场），避免 ToolNode 报
    invalid tool name 把「晚到的代答」变成死胡同。
    """
    if not subscriber_id:
        return None
    from langchain_core.tools import tool

    async def relay_answer(
        worker_session: str, qid: str, choice: str | list[str],
        mapping_reason: str = "",
    ) -> str:
        """Relay YOUR user's answer back to a parked worker question.

        Executed after the question may already have been answered elsewhere;
        the server replies with the authoritative outcome (resumed /
        already_answered / unknown qid) — read the JSON and act on it.
        """
        return await relay_answer_impl(
            subscriber_id, worker_session, qid, choice, mapping_reason
        )

    import pydantic

    class _LooseArgs(pydantic.BaseModel):
        worker_session: str = pydantic.Field(
            description="worker session id (from the ginno_worker_question envelope)"
        )
        qid: str = pydantic.Field(description="question credential (q_…)")
        choice: str | list[str] = pydantic.Field(
            description="the ORIGINAL option text (list for multi-question cards)",
        )
        mapping_reason: str = pydantic.Field(
            default="",
            description="why the user's answer maps to that original option",
        )

    return tool(relay_answer, args_schema=_LooseArgs)


def reconcile_relay() -> None:
    """启动恢复（server lifespan 调）：读盘重建 + 对全部待答条目重注入。

    重注入统一带 reminder 标注（goal ⑥「已问未答 asked_at 重注入时模板
    标注提醒」）：崩溃可能吃掉上次注入，宁可重发——模板幂等（qid 相同、
    指令相同），且 ``_inject_result`` 的 steer 队列按 steer_id 去重不叠
    加。派后台任务：lifespan 里不能 await 网络性注入（可能开 wake turn）。
    """
    load_state()
    if not _PENDING_RELAY:
        return
    from ..server_shared import spawn_bg

    async def _push_all() -> None:
        for ent in list(_PENDING_RELAY.values()):
            await _push_to_subscribers(ent, reminder=True)

    spawn_bg(_push_all())
