"""遗言模式（lastwords，docs/lastwords-design.md）。

sidecar 在被热切换重启**前**把进行中 turn 的关键上下文写成「遗言」落盘
（``<GINNO_HOME>/lastwords/<session_id>.json``）；重启**后**启动扫描按
mode（notify|auto|off）注入回原 session——agent 被唤醒接着干，用户无感
或收到恢复通知。

写入方（三个，层层兜底）：
- ``POST /api/devswap/lastwords/prepare`` — dev_swap.sh switch 段显式预告
  （首选，sidecar 有充分时间落盘，reason="dev-swap"）；
- lifespan finally（SIGTERM 优雅关闭）— 防漏兜底（reason="signal"），
  只做同步快速 IO（<2s 预算）；
- P2 WAL：turn 启动（engine._stream_graph）与工具边界（graph.tools_node）
  增量覆盖写（reason="running"）——SIGKILL 场景最多丢最后一次增量。

消费方：``recover_lastwords()``（server.py lifespan 启动段后台 task）。
恢复回执按恢复 ID 持久化。已失败的原始快照留作诊断，启动扫描会跳过。

快照不调 LLM（重启窗口宝贵）；注入走 tasks/inject 的 ``[task …]`` 首行
族格式（``[lastwords] …`` 前缀），前端 foldTaskEventBlocks 家族识别折叠。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import paths

_log = logging.getLogger("ginno.turn.lastwords")

# Serialize publication and cleanup with the prepare worker thread.
_WRITE_LOCK = threading.RLock()

# TTL：遗言只在重启后短时间内有意义（更晚的注入用户已无上下文），
# 过期文件在启动扫描时直接清理。按 reason 分级（2026-11-07 首跑质量修）：
# running（WAL 初始态，SIGKILL 残留——见模块尾 SIGTERM 诊断）过期更快，
# 显式优雅关闭（dev-swap/signal）保持 1h。
TTL_S = 3600.0
RUNNING_TTL_S = 900.0

# reason 枚举（设计 §2）：dev-swap | signal | running（P2 WAL）。
REASON_DEV_SWAP = "dev-swap"
REASON_SIGNAL = "signal"
REASON_RUNNING = "running"

# 显式优雅关闭的 reason（prepare API / lifespan 兜底），恢复门槛减半。
EXPLICIT_REASONS = (REASON_DEV_SWAP, REASON_SIGNAL)

# settings.json → devswap.lastwords.mode 的合法值（默认 notify）。
# notify：人工会话挂起待确认，子代理会话仅注入提示。
# auto  ：人工会话**重启后也自动续跑**（2026-11-19 恢复语义，用户配置化
#         opt-in——默认不变，选 auto 即接受自动接着干）；subagent 同。
# off   ：不注入不复跑，仅启动扫描清过期文件。
MODES = ("notify", "auto", "off")
DEFAULT_MODE = "notify"

# 快照正文的截断长度（设计 §2：前 500 字）。
HEAD_CHARS = 500

# 恢复价值门槛（2026-11-07 首跑质量修）：consume 时按快照携带的工作量
# 信号过滤「不值得恢复的 turn」——首跑实况：闲聊感叹（0 工具、几秒时长）
# 被当"待恢复工作"注入，纯噪声。保守取值（宁缺勿滥——长任务/多工具
# turn 必须保留）：工具调用 ≥3 或 turn 时长 ≥120s。settings 可覆盖
# devswap.lastwords.min_effort = {"tool_calls": int, "turn_seconds": float}
# （tool_calls=0 即关闭门槛——所有 turn 都放行）。
DEFAULT_MIN_TOOL_CALLS = 3
DEFAULT_MIN_TURN_S = 120.0

# P2 WAL 工具边界写的节流间隔：同 session 两次写至少间隔这么久
# （turn 启动那次不受限——首轮必须立刻有遗言护体）。
TOOL_WRITE_MIN_INTERVAL_S = 5.0

# Keep startup recovery from flooding the model provider after a restart.
STARTUP_RECOVERY_CONCURRENCY = 2

# 无活跃 turn 时的空标记文件名。下划线开头保证不与 session_id（hex）
# 冲突；消费侧看到即删（无 session 可注入，只为审计留痕）。
_MARKER_NAME = "_marker.json"

# 每 session 的上次 WAL 写时间（节流状态，进程内）。
_LAST_WRITE_AT: dict[str, float] = {}

# 出站远程会话（A2A peer）在途 turn 的真实请求头（2026-11-18 修复）：
# sid → {"turn_id", "user_text", "peer", "agent_tail", "last_tool"}。
# 为什么需要它：远端 turn 进行中，本地 checkpoint **没有**当前请求
# （run_remote_turn 只在 turn 结束时 _persist_exchange 落盘问答对），
# snapshot_from_messages 倒序读到的恒是**上一轮**的消息——实锤：
# dev-swap 遗言抓到过期「hi」（user_request_head='hi'、turn_id 是上一轮
# 的），真正在途的 Linux 任务对遗言完全不可见。write_lastwords 组装后
# 用本表按 turn_id 匹配覆盖，账本与快照才指向同一个 turn。
_REMOTE_HEADS: dict[str, dict] = {}
# auto 模式人工会话的「待确认续跑」注册表（2026-11-10 产品化）：
# sid → snap + pending_at。用户点通知进会话后经 resume_pending 确认。
_PENDING_RESUME: dict[str, dict] = {}

# 每 session 的 turn 工作量信号（进程内计数，WAL 写点维护）：
# {"turn_id": str, "started_at": wall-clock ts, "tool_calls": int}。
# 计数在节流**之外**每次更新——节流只丢写文件频率，绝不丢计数。
_EFFORT: dict[str, dict] = {}

# 进程优雅关闭进行中（server.py lifespan finally 首段置位）：此后的
# note_turn_end 不删遗言——asyncio loop teardown 会 cancel 残余 turn
# 协程触发 engine finally 的删除路径，把刚写好的 signal 遗言抹掉。
# （详见模块尾「SIGTERM 兜底诊断」。）
_SHUTTING_DOWN = False


def mark_shutdown() -> None:
    """server.py lifespan finally 首段调用：进入优雅关闭，保护已写遗言。"""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True


def lastwords_dir() -> Path:
    return paths.home() / "lastwords"


def _path(session_id: str) -> Path:
    return lastwords_dir() / f"{session_id}.json"


def mode() -> str:
    """settings.json → devswap.lastwords.mode（缺省/非法回落 notify）。"""
    try:
        raw = json.loads(paths.settings_path().read_text() or "{}")
        m = str(((raw.get("devswap") or {}).get("lastwords") or {}).get("mode") or "")
        return m if m in MODES else DEFAULT_MODE
    except Exception:  # noqa: BLE001 — 设置读取绝不炸调用方
        return DEFAULT_MODE


# --------------------------------------------------------------------------- #
# 快照组装（设计 §2）
# --------------------------------------------------------------------------- #


def _text_of(content: Any) -> str:
    """消息 content（str 或多模态 list）→ 纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(x for x in parts if x)
    return str(content or "")


def _is_injected(m: Any) -> bool:
    """注入消息（task/steer/lastwords/子代理结果带）不作为 user_request 候选。"""
    kw = getattr(m, "additional_kwargs", None) or {}
    return any(
        k in kw
        for k in ("ginno_task_event", "ginno_task_events", "ginno_steer", "ginno_lastwords", "ginno_subagent_result")
    )


def _running_bg_tasks(session_id: str) -> list[str]:
    """该 session 名下 running 态的后台任务 id（重启后 bg_logs 可收割）。"""
    return _bg_tasks_by_status(session_id, running=True)


def _interrupted_bg_tasks(session_id: str) -> list[str]:
    """该 session 名下 **interrupted** 态的后台任务 id（2026-11-16 补）。

    为什么必须收：sidecar 退出会杀掉所有非 detached 子进程，store 把它们
    标为 interrupted（store.py §「app quit」）。**这些恰恰是最该提示用户
    「要不要恢复」的任务**，而原实现只收 running，把它们全过滤了——实测
    索引里 4 个 interrupted（含 SSH 隧道、CI watch）一个都没进遗言带。
    """
    return _bg_tasks_by_status(session_id, running=False)


def _bg_tasks_by_status(session_id: str, *, running: bool) -> list[str]:
    """按状态取该 session 名下的后台任务 id。best effort，异常返回空。"""
    try:
        from .tasks import store as task_store

        want = task_store.STATUS_RUNNING if running else task_store.STATUS_INTERRUPTED
        idx = task_store.read_index()
        return sorted(
            tid
            for tid, rec in idx.items()
            if rec.get("origin_session") == session_id and rec.get("status") == want
        )
    except Exception:  # noqa: BLE001 — best effort
        return []


def snapshot_from_messages(
    session_id: str,
    project_slug: str,
    messages: list[Any],
    *,
    reason: str,
    fingerprint_old: str = "",
) -> dict:
    """从线程消息历史组装结构化快照（不调 LLM，设计 §2）。"""
    user_head = ""
    agent_last = ""
    pending_tools: list[str] = []
    for m in reversed(messages):
        if not user_head and getattr(m, "type", None) == "human" and not _is_injected(m):
            user_head = _text_of(getattr(m, "content", ""))[:HEAD_CHARS]
        if not agent_last and getattr(m, "type", None) == "ai":
            agent_last = _text_of(getattr(m, "content", ""))[:HEAD_CHARS]
            pending_tools = [
                str(tc.get("name") or "")
                for tc in (getattr(m, "tool_calls", None) or [])
                if tc.get("name")
            ]
        if user_head and agent_last:
            break
    return {
        "session_id": session_id,
        "project": project_slug,
        "reason": reason,
        "fingerprint_old": fingerprint_old,
        "user_request_head": user_head,
        "last_agent_message": agent_last,
        "running_tools": pending_tools,
        "bg_tasks": _running_bg_tasks(session_id),
        # 被重启杀掉的后台任务（可重跑）——与 running（可收割）语义不同，分开列
        "bg_interrupted": _interrupted_bg_tasks(session_id),
        "ts": int(time.time()),
    }


def _load_thread_messages(project_slug: str, session_id: str) -> list[Any]:
    """同步读 checkpoint 消息历史（FileCheckpointer.get_tuple，读一个 JSONL）。"""
    try:
        from .checkpointer import FileCheckpointer

        tup = FileCheckpointer(project_slug).get_tuple({"configurable": {"thread_id": session_id}})
        if not tup or not tup.checkpoint:
            return []
        return list((tup.checkpoint.get("channel_values") or {}).get("messages") or [])
    except Exception:  # noqa: BLE001 — 快照 best effort
        return []


def _resolve_slug(session_id: str) -> str | None:
    """活跃 session 的 project_slug：内存 session 表优先，回落磁盘 index。"""
    from . import server_shared as shared

    s = shared._SESSIONS.get(session_id)
    if s and s.get("project_slug"):
        return str(s["project_slug"])
    try:
        from .session_meta import _find_meta

        found = _find_meta(session_id)
        if found and found[1]:
            return found[1]
    except Exception:  # noqa: BLE001
        pass
    return None


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _publish_snapshot(sid: str, snap: dict, effort: dict | None) -> None:
    """Publish under _WRITE_LOCK after the caller verifies ownership."""
    if effort:
        snap["turn_id"] = effort["turn_id"]
        snap["registered_turn_id"] = effort["registered_turn_id"]
        snap["effort"] = {key: effort[key] for key in ("turn_id", "started_at", "tool_calls")}
        if "request_head" in effort:
            snap["user_request_head"] = effort["request_head"]
    _write_atomic(_path(sid), snap)
    _LAST_WRITE_AT[sid] = time.monotonic()


def write_lastwords(
    reason: str,
    *,
    fingerprint_old: str = "",
    sessions: dict[str, str] | None = None,
    messages_for=None,
) -> int:
    """遍历活跃 turn 写遗言，返回写入条数（活跃 session 数）。

    ``sessions`` 缺省取 ``server_shared._RUNNING_TURNS``（session_id →
    turn_id）；``messages_for`` 可注入消息加载器（测试用）。无活跃 turn
    且 reason 是显式预告（dev-swap/signal）时写空标记 ``_marker.json``
    ——reason 字段仍记，消费侧见之即删。
    """
    from . import server_shared as shared

    live = sessions if sessions is not None else dict(shared._RUNNING_TURNS)
    loader = messages_for or _load_thread_messages
    count = 0
    for sid, turn_id in live.items():
        slug = _resolve_slug(sid)
        if not slug:
            _log.info("lastwords_skip_no_slug session=%s", sid)
            continue
        with _WRITE_LOCK:
            effort = _EFFORT.get(sid)
            remote = _REMOTE_HEADS.get(sid)
        try:
            msgs = loader(slug, sid)
            if effort and effort["registered_turn_id"] == turn_id:
                if "request_head" in effort:
                    msgs = _messages_from_turn(msgs, effort["turn_id"], turn_id)
            elif not remote or remote["turn_id"] != turn_id:
                # A WS invoke registers before entering the engine. Use a
                # matching checkpoint when opening has not yet supplied input.
                msgs = _messages_from_turn(msgs, turn_id, turn_id)
                if not msgs:
                    continue
            snap = snapshot_from_messages(
                sid, slug, msgs, reason=reason, fingerprint_old=fingerprint_old,
            )
            snap["turn_id"] = turn_id
            with _WRITE_LOCK:
                # The checkpoint read above can race a finish or another start.
                if sessions is None and shared._RUNNING_TURNS.get(sid) != turn_id:
                    continue
                if _EFFORT.get(sid) is not effort or _REMOTE_HEADS.get(sid) is not remote:
                    continue
                if remote and remote["turn_id"] == turn_id:
                    snap.update(
                        user_request_head=remote["user_text"][:HEAD_CHARS],
                        last_agent_message=remote["agent_tail"][-HEAD_CHARS:],
                        running_tools=[remote["last_tool"]] if remote["last_tool"] else [],
                        remote=True,
                        peer=remote["peer"],
                    )
                owned_effort = effort if effort and effort["registered_turn_id"] == turn_id else None
                _publish_snapshot(sid, snap, owned_effort)
                count += 1
        except Exception:  # noqa: BLE001 — 单 session 失败不拖累其余
            _log.exception("lastwords_write_failed session=%s", sid)
    if not count and reason in (REASON_DEV_SWAP, REASON_SIGNAL):
        try:
            _write_atomic(
                lastwords_dir() / _MARKER_NAME,
                {
                    "reason": reason,
                    "sessions": [],
                    "fingerprint_old": fingerprint_old,
                    "ts": int(time.time()),
                },
            )
        except Exception:  # noqa: BLE001
            _log.exception("lastwords_marker_write_failed")
    if count or reason in (REASON_DEV_SWAP, REASON_SIGNAL):
        _log.info(
            "lastwords_written reason=%s count=%d sessions=%s",
            reason,
            count,
            ",".join(sorted(live)) or "-",
        )
    return count


# --------------------------------------------------------------------------- #
# P2 WAL：turn 生命周期钩子（engine._stream_graph / graph.tools_node 调用）
# --------------------------------------------------------------------------- #


def _reset_effort(session_id: str, turn_id: str, registered_turn_id: str) -> dict:
    effort = {
        "turn_id": turn_id,
        "registered_turn_id": registered_turn_id,
        "started_at": time.time(),
        "tool_calls": 0,
    }
    _EFFORT[session_id] = effort
    return effort


def _messages_from_turn(messages: list[Any], turn_id: str, registered_turn_id: str) -> list[Any]:
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if getattr(msg, "type", None) == "human" and getattr(msg, "id", None) in (
            turn_id, registered_turn_id,
        ):
            return messages[i:]
    return []


def _write_remote_snapshot(session_id: str, reason: str) -> None:
    """从执行器注册表拼远端 turn 快照并落盘（best-effort，绝不抛）。"""
    reg = _REMOTE_HEADS.get(session_id) or {}
    slug = str(reg.get("project_slug") or "") or _resolve_slug(session_id)
    if not slug:
        return
    snap = {
        "session_id": session_id,
        "project": slug,
        "reason": reason,
        "fingerprint_old": "",
        "user_request_head": str(reg.get("user_text") or "")[:HEAD_CHARS],
        "last_agent_message": str(reg.get("agent_tail") or "")[-HEAD_CHARS:],
        "running_tools": [str(reg["last_tool"])] if reg.get("last_tool") else [],
        "bg_tasks": [],
        "bg_interrupted": [],
        "ts": int(time.time()),
        "remote": True,
    }
    if reg.get("peer"):
        snap["peer"] = str(reg["peer"])
    snap["turn_id"] = str(reg.get("turn_id") or "")
    _publish_snapshot(session_id, snap, _EFFORT.get(session_id))


def note_remote_turn_start(
    session_id: str,
    turn_id: str,
    project_slug: str | None,
    user_text: str,
    *,
    peer: str = "",
) -> None:
    """出站远程 turn 启动：注册真实请求头 + 写初始 WAL 遗言（reason=running）。

    与本地 ``note_turn_start`` 对位——本地版从 checkpoint 回落上一条用户
    消息，远端版**必须**用执行器手里的 user_text：本地 checkpoint 在 turn
    结束前没有它（见 ``_REMOTE_HEADS`` 注释）。幂等：同 turn 重复调用
    只刷新；新 turn 直接覆盖旧注册（顺带收编残留）。
    """
    if not session_id or not user_text:
        return
    try:
        with _WRITE_LOCK:
            _REMOTE_HEADS[session_id] = {
                "turn_id": turn_id,
                "user_text": user_text,
                "peer": peer,
                "agent_tail": "",
                "last_tool": "",
                "project_slug": project_slug,
            }
            _reset_effort(session_id, turn_id, turn_id)
            _write_remote_snapshot(session_id, REASON_RUNNING)
    except Exception:  # noqa: BLE001 — 遗言绝不拖累 turn 本体
        _log.exception("lastwords_remote_turn_start_failed session=%s", session_id)


def note_remote_progress(
    session_id: str,
    turn_id: str,
    *,
    tool_name: str | None = None,
    agent_tail: str | None = None,
) -> None:
    """远端 turn 进行中：工具边界计数 + 流式尾部累积，节流写盘。

    与本地 ``note_tool_boundary`` 对位。token.delta 只更新内存尾部（零
    IO，每帧都调）；tool 边界 bump effort 并按 ``TOOL_WRITE_MIN_INTERVAL_S``
    节流覆盖写 WAL——计数每次都加，节流只限写盘频率。
    """
    try:
        with _WRITE_LOCK:
            reg = _REMOTE_HEADS.get(session_id)
            if reg is None or reg["turn_id"] != turn_id:
                return
            if agent_tail is not None:
                reg["agent_tail"] = (reg["agent_tail"] + agent_tail)[-2000:]
            if tool_name:
                _EFFORT[session_id]["tool_calls"] += 1
                reg["last_tool"] = tool_name
                last = _LAST_WRITE_AT.get(session_id, -float("inf"))
                if time.monotonic() - last >= TOOL_WRITE_MIN_INTERVAL_S:
                    _write_remote_snapshot(session_id, REASON_RUNNING)
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_remote_progress_failed session=%s", session_id)


def note_remote_turn_end(
    session_id: str, turn_id: str, *, outcome: str | None = None, interrupted: bool = False,
) -> None:
    with _WRITE_LOCK:
        reg = _REMOTE_HEADS.get(session_id)
        if reg is not None and reg["turn_id"] != turn_id:
            return
        _REMOTE_HEADS.pop(session_id, None)
        note_turn_end(session_id, turn_id, outcome=outcome, interrupted=interrupted)


def note_turn_start(
    session_id: str,
    turn_id: str,
    project_slug: str | None,
    *,
    input_messages: list[Any] | None = None,
    registered_turn_id: str | None = None,
) -> None:
    """Capture the submitted input before the first checkpoint is committed."""
    if not session_id:
        return
    try:
        slug = project_slug or _resolve_slug(session_id)
        if not slug:
            return
        logical_id = registered_turn_id or turn_id
        messages = input_messages
        if messages is None:
            messages = _load_thread_messages(slug, session_id)
            if registered_turn_id is not None:
                messages = _messages_from_turn(messages, turn_id, logical_id)
        snap = snapshot_from_messages(session_id, slug, messages, reason=REASON_RUNNING)
        for msg in reversed(messages):
            if getattr(msg, "id", None) not in (turn_id, logical_id):
                continue
            if getattr(msg, "type", None) != "human":
                continue
            marker = (getattr(msg, "additional_kwargs", None) or {}).get("ginno_lastwords") or {}
            snap["user_request_head"] = str(
                marker.get("request_head", _text_of(msg.content))
            )[:HEAD_CHARS]
            break
        with _WRITE_LOCK:
            prior = _EFFORT.get(session_id)
            effort = _reset_effort(session_id, turn_id, logical_id)
            effort["request_head"] = snap["user_request_head"]
            if prior and prior["registered_turn_id"] == logical_id:
                effort["started_at"] = prior["started_at"]
                effort["tool_calls"] = prior["tool_calls"]
                if not messages and "request_head" in prior:
                    effort["request_head"] = prior["request_head"]
            _publish_snapshot(session_id, snap, effort)
    except Exception:  # noqa: BLE001 — 遗言绝不拖累 turn 本体
        _log.exception("lastwords_turn_start_failed session=%s", session_id)


def note_tool_boundary(
    session_id: str, turn_id: str, project_slug: str | None, messages: list[Any]
) -> None:
    """Count every tool batch and coalesce writes for the current attempt."""
    if not session_id:
        return
    try:
        with _WRITE_LOCK:
            effort = _EFFORT.get(session_id)
            if effort is not None and effort["turn_id"] != turn_id:
                return
            if effort is None:
                effort = _reset_effort(session_id, turn_id, turn_id)
            effort["tool_calls"] += 1
            last = _LAST_WRITE_AT.get(session_id)
            if last is not None and time.monotonic() - last < TOOL_WRITE_MIN_INTERVAL_S:
                return
            slug = project_slug or _resolve_slug(session_id)
            if slug:
                snap = snapshot_from_messages(session_id, slug, messages, reason=REASON_RUNNING)
                _publish_snapshot(session_id, snap, effort)
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_tool_boundary_failed session=%s", session_id)


def note_turn_end(
    session_id: str, turn_id: str, *, outcome: str | None = None, interrupted: bool = False
) -> None:
    """Mark a proven terminal attempt before cleanup, or retain its WAL."""
    if not session_id:
        return
    try:
        with _WRITE_LOCK:
            effort = _EFFORT.get(session_id)
            if effort and effort["turn_id"] != turn_id:
                return
            _LAST_WRITE_AT.pop(session_id, None)
            _EFFORT.pop(session_id, None)
            if interrupted or (_SHUTTING_DOWN and outcome is None):
                return
            path = _path(session_id)
            snap = _read_object(path)
            if snap and snap.get("turn_id") == turn_id:
                if outcome:
                    _write_atomic(path, {**snap, "terminal": outcome})
                _drop(path)
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_turn_end_failed session=%s", session_id)


# --------------------------------------------------------------------------- #
# 消费（启动扫描）
# --------------------------------------------------------------------------- #


def min_effort() -> tuple[int, float]:
    """settings.json → devswap.lastwords.min_effort（缺省回落默认门槛）。

    ``tool_calls=0`` 显式关闭工具维度门槛（时长维度仍生效）。
    """
    try:
        raw = json.loads(paths.settings_path().read_text() or "{}")
        me = ((raw.get("devswap") or {}).get("lastwords") or {}).get("min_effort") or {}
        tc = int(me.get("tool_calls", DEFAULT_MIN_TOOL_CALLS))
        ts = float(me.get("turn_seconds", DEFAULT_MIN_TURN_S))
        return max(tc, 0), max(ts, 0.0)
    except Exception:  # noqa: BLE001 — 设置读取绝不炸消费侧
        return DEFAULT_MIN_TOOL_CALLS, DEFAULT_MIN_TURN_S


def _worth_restoring(snap: dict) -> bool:
    """恢复价值门槛（2026-11-07 首跑质量修）：低工作量 turn 静默丢弃。

    - 信号：WAL 写点维护的 ``effort``（turn 累计工具调用数 + turn 起始
      时间戳）；快照缺该字段（旧版本文件 / 信号丢失）时**保守放行**——
      门槛只用来过滤「明确低价值」的（首跑实况：闲聊感叹被当待恢复
      工作注入），不可判定的不误杀。
    - 保守阈值（默认）：工具调用 ≥3 **或** turn 时长 ≥120s——多轮工具
      循环或长思考 turn 必须保留。
    - 显式优雅关闭（dev-swap/signal）门槛减半（向上取整）：明确的
      重启场景更值得恢复。
    """
    eff = snap.get("effort")
    if not isinstance(eff, dict) or not eff:
        return True
    tool_calls = int(eff.get("tool_calls") or 0)
    started = float(eff.get("started_at") or 0)
    elapsed = max(0.0, float(snap.get("ts") or 0) - started)
    min_tc, min_s = min_effort()
    if str(snap.get("reason") or "") in EXPLICIT_REASONS:
        min_tc = max(1, (min_tc + 1) // 2)
        min_s = min_s / 2.0
    return tool_calls >= min_tc or elapsed >= min_s


def _elapsed(snap: dict) -> float:
    started = (snap.get("effort") or {}).get("started_at")
    return max(0.0, float(snap.get("ts") or 0) - float(started)) if started else 0.0


def consume_lastwords(*, now: float | None = None) -> list[dict]:
    """Collect valid candidates and discard only the snapshot examined."""
    now = time.time() if now is None else now
    out: list[dict] = []
    for path in sorted(lastwords_dir().glob("*.json")):
        with _WRITE_LOCK:
            snap = _read_object(path)
            if snap is None:
                _log.warning("lastwords_corrupt_dropped file=%s", path.name)
                _drop(path)
                continue
            if path.name == _MARKER_NAME:
                _log.info("lastwords_marker_consumed reason=%s", snap.get("reason"))
                _drop(path)
                continue
            if snap.get("session_id") != path.stem or snap.get("terminal"):
                _drop(path)
                continue
            if snap.get("failed_recovery_id"):
                continue
            try:
                receipt = _receipt_for_snapshot(snap)
                if receipt and receipt["status"] == "failed":
                    continue
                pending_at = receipt.get("pending_at") if receipt and receipt["status"] == "pending" else None
                freshness = float(pending_at) if pending_at else float(snap.get("ts") or 0)
                ttl = RUNNING_TTL_S if not pending_at and snap.get("reason") == REASON_RUNNING else TTL_S
                if now - freshness > ttl:
                    _log.info("lastwords_expired_dropped session=%s age=%.0fs", path.stem, now - freshness)
                    _drop(path)
                    continue
                if not receipt and not _worth_restoring(snap):
                    eff = snap.get("effort") or {}
                    _log.info(
                        "lastwords_low_effort_dropped session=%s reason=%s tool_calls=%s elapsed=%.0fs",
                        path.stem, snap.get("reason"), eff.get("tool_calls"), _elapsed(snap),
                    )
                    _drop(path)
                    continue
            except (TypeError, ValueError, KeyError, AttributeError):
                _log.warning("lastwords_corrupt_dropped file=%s", path.name)
                _drop(path)
                continue
            out.append(snap)
    return out


def _drop(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_drop_failed file=%s", p.name)


def drop_lastwords(session_id: str) -> None:
    """恢复流程的 exactly-once 收口：注入终态后删文件。"""
    _drop(_path(session_id))


def is_subagent_session(session: dict | None) -> bool:
    """会话是否 subagent 型（headless 子会话，2026-11-07 追加放宽的判据）。

    判据 = 会话元数据 ``type == "subagent"``（sessions.py 创建/重启重建
    session dict 时从落盘 meta 恢复，与 subagent-design.md §4 的
    parent_session_id/depth 同源——勿凭会话名/标题猜测）。重建 dict 缺
    字段时回落磁盘 meta 双保险。
    """
    if not session:
        return False
    if str(session.get("type") or "") == "subagent":
        return True
    sid = str(session.get("session_id") or "")
    if not sid:
        return False
    try:
        from .session_meta import _find_meta

        found = _find_meta(sid)
        return bool(found and str((found[0] or {}).get("type") or "") == "subagent")
    except Exception:  # noqa: BLE001
        return False


def reconcile_bg_tasks(snap: dict) -> None:
    """注入前实时核对 bg_tasks（2026-11-07 首跑质量修）。

    首跑实况：「1 个可收割」的后台任务实际早已完成——快照是重启前
    某个工具边界写的，bg 任务状态在快照落盘与注入之间继续演化。
    对照 /api/tasks 的数据源（tasks/store index）：已结束的移除；全部
    结束则清空（render 侧自然省略该段）。核对源不可达时保留原列表并
    打 ``bg_tasks_unknown`` 标（render 加「状态未知」后缀）——宁可多
    提示收割一次，不静默吞掉可能在跑的任务。
    """
    bg = snap.get("bg_tasks") or []
    if not bg and not (snap.get("bg_interrupted") or []):
        return
    try:
        from .tasks import store as task_store

        idx = task_store.read_index()
    except Exception:  # noqa: BLE001 — 核对失败降级，不拦注入
        _log.warning(
            "lastwords_bg_reconcile_unavailable session=%s tasks=%d",
            snap.get("session_id"),
            len(bg),
        )
        snap["bg_tasks_unknown"] = True
        return
    live = [
        t
        for t in bg
        if str((idx.get(str(t)) or {}).get("status") or "") == task_store.STATUS_RUNNING
    ]
    if len(live) != len(bg):
        _log.info(
            "lastwords_bg_reconciled session=%s kept=%d dropped=%d",
            snap.get("session_id"),
            len(live),
            len(bg) - len(live),
        )
    snap["bg_tasks"] = live
    # interrupted 列表同样实时核对（2026-11-16）：仍 interrupted 的才提示重跑；
    # 已被认领/重跑成别的状态就移除，避免提示用户去重跑一个已在跑的任务。
    dead = snap.get("bg_interrupted") or []
    if dead:
        still = [
            t
            for t in dead
            if str((idx.get(str(t)) or {}).get("status") or "")
            == task_store.STATUS_INTERRUPTED
        ]
        if len(still) != len(dead):
            _log.info(
                "lastwords_bg_interrupted_reconciled session=%s kept=%d dropped=%d",
                snap.get("session_id"), len(still), len(dead) - len(still),
            )
        snap["bg_interrupted"] = still


def render_band(snap: dict) -> str:
    """注入文本首行（``[task finished]`` 同族格式，前端折叠识别）。"""
    head = str(snap.get("user_request_head") or "").strip().replace("\n", " ")
    summary = head[:80] + ("…" if len(head) > 80 else "")
    reason = str(snap.get("reason") or "")
    fp = str(snap.get("fingerprint_old") or "")
    fp_note = f" · 旧指纹 {fp[:12]}" if fp else ""
    bg = snap.get("bg_tasks") or []
    bg_note = ""
    if bg:
        suffix = "，状态未知" if snap.get("bg_tasks_unknown") else ""
        bg_note = f" · 后台任务 {len(bg)} 个可收割(bg_logs{suffix})"
    dead = snap.get("bg_interrupted") or []
    dead_note = f" · {len(dead)} 个后台任务被中断(可重跑)" if dead else ""
    return f"[lastwords] {reason} 重启恢复：{summary}{fp_note}{bg_note}{dead_note}"


def render_continuation(snap: dict) -> str:
    """auto 模式 headless 续跑 turn 的用户侧输入（设计 §3 模板）。"""
    head = str(snap.get("user_request_head") or "").strip()
    agent = str(snap.get("last_agent_message") or "").strip()
    bg = snap.get("bg_tasks") or []
    lines = [
        "[lastwords] 热切换重启后的自动续跑：runtime 进程在你上次工作中被重启，"
        "以下是遗留上下文快照，请从中断处继续完成任务（先核对已完成部分，不要重做）。",
        "",
        f"原始请求：{head or '（快照缺失）'}",
        f"最后进展：{agent or '（尚无回复）'}",
    ]
    if bg:
        lines.append(
            f"后台任务 {', '.join(map(str, bg))} 可能仍在跑或已结束，用 bg_logs 收割结果。"
        )
    dead = snap.get("bg_interrupted") or []
    if dead:
        lines.append(
            f"另有 {len(dead)} 个后台任务被本次重启中断（{', '.join(map(str, dead))}）："
            "它们**已不在运行**，需要时先问用户是否重跑，不要擅自重启。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 恢复流程（server.py lifespan 启动段后台 task 调用）
# --------------------------------------------------------------------------- #



_TERMINAL_RECEIPTS = {"completed", "failed", "stopped", "dismissed", "stale", "notified"}


def _receipt_path(session_id: str) -> Path:
    return lastwords_dir() / "receipts" / f"{session_id}.json"


def _source_id(snap: dict) -> str:
    turn = snap.get("registered_turn_id") or snap.get("turn_id") or f"legacy-{snap.get('ts')}"
    return uuid.uuid5(uuid.NAMESPACE_URL, f"ginno/lastwords/{snap['session_id']}/{turn}").hex


def _receipt_for_snapshot(snap: dict) -> dict | None:
    receipt = _read_object(_receipt_path(snap["session_id"]))
    if receipt and receipt.get("recovery_id") and receipt.get("status"):
        turn = snap.get("registered_turn_id") or snap.get("turn_id")
        recovery_id = str(receipt["recovery_id"])
        if len(recovery_id) != 32 or any(c not in "0123456789abcdef" for c in recovery_id):
            return None
        if receipt.get("source_id") == _source_id(snap) or (turn and receipt.get("resume_turn_id") == turn):
            return receipt
    return None


def _same_snapshot(snap: dict) -> bool:
    current = _read_object(_path(snap["session_id"]))
    return bool(
        current and current.get("session_id") == snap["session_id"] and not current.get("terminal")
        and current.get("turn_id") == snap.get("turn_id") and _source_id(current) == _source_id(snap)
    )


def _drop_candidate(snap: dict) -> None:
    with _WRITE_LOCK:
        if _same_snapshot(snap):
            drop_lastwords(snap["session_id"])


def _save_receipt(snap: dict, status: str, **fields: Any) -> dict:
    sid = snap["session_id"]
    old = _receipt_for_snapshot(snap) or {}
    source = _source_id(snap)
    receipt = {
        **old,
        "session_id": sid,
        "recovery_id": old.get("recovery_id") or source,
        "source_id": source,
        "reason": snap.get("reason", ""),
        "status": status,
        "updated_at": max(time.time(), float(old.get("updated_at") or 0) + 0.000001),
        **fields,
    }
    if old and all(old.get(key) == value for key, value in receipt.items() if key != "updated_at"):
        return old
    _write_atomic(_receipt_path(sid), receipt)
    _log.info(
        "lastwords_recovery session=%s recovery=%s status=%s reason=%s diagnostic=%s",
        sid, receipt["recovery_id"], status, receipt["reason"], receipt.get("diagnostic") or "-",
    )
    return receipt


def list_receipts() -> list[dict]:
    cutoff = time.time() - TTL_S
    receipts = []
    for path in (lastwords_dir() / "receipts").glob("*.json"):
        receipt = _read_object(path)
        if not receipt or receipt.get("session_id") != path.stem:
            continue
        updated = receipt.get("updated_at")
        if not isinstance(updated, (int, float)) or updated < cutoff:
            continue
        if receipt.get("recovery_id") and receipt.get("status") not in (None, "pending"):
            receipts.append(_public_receipt(receipt))
    return sorted(receipts, key=lambda item: item["updated_at"])[-32:]


def _public_receipt(receipt: dict) -> dict:
    return {key: receipt[key] for key in ("session_id", "recovery_id", "status", "updated_at")}


def _retained_path(receipt: dict) -> Path:
    return lastwords_dir() / "retained" / f"{receipt['recovery_id']}.json"


def _retain_source(snap: dict, receipt: dict) -> None:
    with _WRITE_LOCK:
        path = _retained_path(receipt)
        if not path.exists():
            stored = _read_object(_path(snap["session_id"])) if _same_snapshot(snap) else None
            _write_atomic(path, stored or snap)


async def _finish_recovery(snap: dict, outcome: str, diagnostic: str = "") -> bool:
    receipt = _save_receipt(snap, outcome, diagnostic=diagnostic)
    retained = _retained_path(receipt)
    if outcome == "failed":
        # The attempt's WAL may have replaced the source and then been cleaned
        # by the turn itself. Keep the original even when another turn owns WAL.
        with _WRITE_LOCK:
            original = _read_object(retained)
            live = _read_object(_path(snap["session_id"]))
            if original and (live is None or _same_snapshot(snap)
                             or live.get("turn_id") == receipt.get("resume_turn_id")):
                _write_atomic(_path(snap["session_id"]), {
                    **original, "failed_recovery_id": receipt["recovery_id"],
                })
    elif outcome != "awaiting_input":
        _drop_candidate(snap)
        _drop(retained)
    await _publish_receipt(receipt)
    return outcome == "completed"


async def _publish_receipt(receipt: dict) -> None:
    from . import server_shared as shared

    event = ("runtime.lastwords_restored" if receipt["status"] in ("completed", "notified")
             else "runtime.lastwords_status")
    try:
        await shared._push_global_event(event, _public_receipt(receipt))
    except Exception:  # noqa: BLE001 — REST can rehydrate the durable receipt
        _log.exception("lastwords_receipt_broadcast_failed session=%s", receipt["session_id"])


def pending_resume_snapshot(session_id: str) -> dict | None:
    snap = _PENDING_RESUME.get(session_id)
    if not snap:
        return None
    expired = time.time() - float(snap.get("pending_at") or 0) > TTL_S
    if expired or (snap.get("recovery_id") and not _same_snapshot(snap)):
        _PENDING_RESUME.pop(session_id, None)
        if expired:
            _drop_candidate({**snap, "session_id": session_id})
        return None
    return snap


def _pending_payload(snap: dict) -> dict:
    head = str(snap.get("user_request_head") or "")[:160]
    return {
        "session_id": snap["session_id"],
        "recovery_id": snap.get("recovery_id"),
        "head": head,
        "user_request_head": head,
        "reason": snap.get("reason", ""),
        "remote": bool(snap.get("remote")),
        "uncertain": bool(snap.get("uncertain")),
        "bg_running": len(snap.get("bg_tasks") or []),
        "bg_interrupted": len(snap.get("bg_interrupted") or []),
        "bg_interrupted_ids": list(snap.get("bg_interrupted") or []),
        "tool_calls": (snap.get("effort") or {}).get("tool_calls", 0),
        "elapsed_s": _elapsed(snap),
        "pending_at": float(snap.get("pending_at") or 0),
        "updated_at": float(snap.get("updated_at") or 0),
    }


def list_pending(*, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out = []
    for sid, snap in list(_PENDING_RESUME.items()):
        if now - float(snap.get("pending_at") or 0) > TTL_S:
            _PENDING_RESUME.pop(sid, None)
            _drop_candidate({**snap, "session_id": sid})
        elif not snap.get("recovery_id") or _same_snapshot(snap):
            out.append(_pending_payload({**snap, "session_id": sid}))
        else:
            _PENDING_RESUME.pop(sid, None)
    return sorted(out, key=lambda item: item["pending_at"])


def _parked(session: dict) -> bool:
    saver = getattr(session.get("graph"), "checkpointer", None)
    get_pending = getattr(saver, "get_pending_interrupt", None)
    return bool(get_pending and get_pending({"configurable": {"thread_id": session["session_id"]}}))


def awaiting_recovery_snapshot(session_id: str) -> dict | None:
    snap = _read_object(_path(session_id))
    receipt = _receipt_for_snapshot(snap) if snap else None
    if snap and receipt and receipt["status"] == "awaiting_input":
        return {**snap, "resume_turn_id": receipt.get("resume_turn_id")}
    return None


async def finish_awaiting_recovery(snap: dict, outcome: str) -> None:
    try:
        await _finish_recovery(snap, outcome, "execution_failed" if outcome == "failed" else "")
    except Exception:  # noqa: BLE001 — the normal interrupt resume has already settled
        _log.exception("lastwords_parked_receipt_failed session=%s", snap.get("session_id"))


async def note_parked_stop(session_id: str, turn_id: str) -> None:
    """Retire the parked WAL from the existing stop path under the turn lock."""
    try:
        with _WRITE_LOCK:
            snap = _read_object(_path(session_id))
            if not snap or snap.get("session_id") != session_id or snap.get("failed_recovery_id"):
                return
            if turn_id and turn_id not in (snap.get("turn_id"), snap.get("registered_turn_id")):
                return
            receipt = _receipt_for_snapshot(snap)
            if exec_id := snap.get("turn_id"):
                note_turn_end(session_id, exec_id, outcome="stopped")
            else:
                _drop_candidate(snap)
        if receipt and receipt["status"] == "awaiting_input":
            await _finish_recovery(snap, "stopped")
    except Exception:  # noqa: BLE001 — stop was already applied to the checkpoint
        _log.exception("lastwords_parked_stop_failed session=%s", session_id)


async def _run_recovery_job(snap: dict) -> bool:
    from . import server_shared as shared
    from .api.sessions import _ensure_session
    from .api.stream.remote_turn import remote_peer_of

    sid = snap["session_id"]
    async with shared._turn_lock(sid):
        receipt = _receipt_for_snapshot(snap)
        if not receipt or receipt["status"] != "queued":
            return False
        diagnostic = ""
        try:
            if not _same_snapshot(snap) or (session := _ensure_session(sid)) is None:
                outcome = "stale"
            elif _parked(session):
                outcome = "awaiting_input"
            elif shared.turn_gate_busy(sid):
                outcome = "stale"
            else:
                snap = {**snap, "remote": bool(remote_peer_of(session))}
                if snap["remote"] and receipt.get("via") == "auto":
                    await _register_pending(session, snap, receipt)
                    return False
                _retain_source(snap, receipt)
                current = _save_receipt(snap, "running", resume_turn_id=f"lw-{uuid.uuid4().hex}")
                outcome = await _run_lastwords_turn(session, {**snap, **current})
                if outcome not in ("completed", "failed", "stopped", "awaiting_input"):
                    outcome = "failed"
                if outcome == "failed":
                    diagnostic = "remote_execution_failed" if snap.get("remote") else "execution_failed"
        except asyncio.CancelledError:
            _save_receipt(snap, "interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 — retain the failure receipt
            _log.exception("lastwords_run_failed session=%s", sid)
            outcome = "failed"
            diagnostic = type(exc).__name__
        return await _finish_recovery(snap, outcome, diagnostic)


async def resume_pending(session_id: str, recovery_id: str | None = None) -> dict:
    from . import server_shared as shared
    from .api.sessions import _ensure_session

    lock = shared._turn_lock(session_id)
    if lock.locked():
        return {"ok": False, "error": "session_busy"}
    async with lock:
        snap = pending_resume_snapshot(session_id)
        if not snap:
            return {"ok": False, "error": "no_pending"}
        if recovery_id is not None and recovery_id != snap.get("recovery_id"):
            return {"ok": False, "error": "stale_recovery"}
        session = _ensure_session(session_id)
        if not session:
            return {"ok": False, "error": "no_session"}
        if _parked(session):
            receipt = _save_receipt(snap, "awaiting_input")
            _retain_source(snap, receipt)
            _PENDING_RESUME.pop(session_id, None)
            await _publish_receipt(receipt)
            return {"ok": False, "error": "awaiting_input"}
        if shared.turn_gate_busy(session_id):
            return {"ok": False, "error": "session_busy"}
        receipt = _save_receipt(snap, "queued", via="manual")
        _PENDING_RESUME.pop(session_id, None)
        shared.spawn_bg(_run_recovery_job(snap))
        return {
            "ok": True, "recovery_id": receipt["recovery_id"],
            "status": "queued", "updated_at": receipt["updated_at"],
        }


def dismiss_pending(session_id: str, recovery_id: str | None = None) -> dict:
    snap = pending_resume_snapshot(session_id)
    if not snap:
        return {"ok": True, "had": False}
    if recovery_id is not None and recovery_id != snap.get("recovery_id"):
        return {"ok": False, "error": "stale_recovery"}
    receipt = _save_receipt(snap, "dismissed")
    _drop(_retained_path(receipt))
    _PENDING_RESUME.pop(session_id, None)
    _drop_candidate(snap)
    return {"ok": True, "had": True, **_public_receipt(receipt)}


async def confirm_dismissal(session_id: str, recovery_id: str) -> dict:
    result = dismiss_pending(session_id, recovery_id)
    if result.get("had"):
        await _publish_receipt(result)
    return result


async def _register_pending(session: dict, snap: dict, prior: dict) -> None:
    from . import server_shared as shared

    uncertain = bool(prior.get("uncertain") or prior.get("status") in ("running", "interrupted"))
    receipt = _save_receipt(
        snap, "pending", pending_at=prior.get("pending_at") or time.time(), uncertain=uncertain,
    )
    snap = {**snap, **receipt}
    await _inject_lastwords_message(session, snap)
    _PENDING_RESUME[snap["session_id"]] = snap
    await shared._push_global_event("runtime.lastwords_pending", _pending_payload(snap))


def _legacy_snapshot_superseded(snap: dict) -> bool:
    """Older builds could leave a WAL behind after the session last finished."""
    if snap.get("registered_turn_id"):
        return False
    from .session_meta import _find_meta

    found = _find_meta(snap["session_id"])
    return bool(found and float(found[0].get("last_finished") or 0) >= float(snap.get("ts") or 0) + 1)


async def recover_lastwords() -> int:
    """Register all candidates before running automatic recoveries in parallel.

    The lifespan owns this background task, so shutdown cancels its children.
    Queued candidates keep their snapshots and can be picked up on the next start.
    """
    from . import server_shared as shared
    from .api.sessions import _ensure_session
    from .api.stream.remote_turn import remote_peer_of

    selected_mode = mode()
    restored = 0
    jobs: list[tuple[dict, dict]] = []
    for snap in consume_lastwords():
        sid = snap["session_id"]
        try:
            prior = _receipt_for_snapshot(snap) or {}
            if selected_mode == "off" or prior.get("status") in _TERMINAL_RECEIPTS:
                _drop_candidate(snap)
                continue
            if _legacy_snapshot_superseded(snap):
                _drop_candidate(snap)
                _log.info("lastwords_superseded session=%s", sid)
                continue
            session = _ensure_session(sid)
            if session is None:
                _drop_candidate(snap)
                _log.info("lastwords_dropped_no_session session=%s", sid)
                continue
            lock = shared._turn_lock(sid)
            if lock.locked():
                continue
            async with lock:
                if not _same_snapshot(snap):
                    continue
                if _parked(session):
                    receipt = _save_receipt(snap, "awaiting_input")
                    _retain_source(snap, receipt)
                    _PENDING_RESUME.pop(sid, None)
                    await _publish_receipt(receipt)
                    continue
                if shared.turn_gate_busy(sid):
                    continue
                reconcile_bg_tasks(snap)
                remote = bool(remote_peer_of(session))
                snap["remote"] = remote
                prior_status = prior.get("status")
                manual_claim = prior_status == "queued" and prior.get("via") == "manual"
                if prior_status in ("pending", "running", "interrupted", "awaiting_input"):
                    await _register_pending(session, snap, prior)
                elif manual_claim or (selected_mode == "auto" and not remote):
                    receipt = _save_receipt(snap, "queued", via="manual" if manual_claim else "auto")
                    jobs.append((snap, receipt))
                elif remote or not is_subagent_session(session):
                    await _register_pending(session, snap, prior)
                else:
                    await _inject_lastwords_message(session, {**snap, "recovery_id": _source_id(snap)})
                    receipt = _save_receipt(snap, "notified")
                    _drop_candidate(snap)
                    await _publish_receipt(receipt)
                    restored += 1
        except Exception:  # noqa: BLE001 — isolate one broken candidate
            _log.exception("lastwords_recover_failed session=%s", sid)

    if not jobs:
        return restored

    slots = asyncio.Semaphore(STARTUP_RECOVERY_CONCURRENCY)

    async def run(snap: dict, receipt: dict) -> bool:
        await _publish_receipt(receipt)
        async with slots:
            try:
                return await _run_recovery_job(snap)
            except Exception:  # noqa: BLE001 — another session must still recover
                _log.exception("lastwords_recover_failed session=%s", snap["session_id"])
                return False

    async with asyncio.TaskGroup() as group:
        dispatched = [group.create_task(run(snap, receipt)) for snap, receipt in jobs]
    return restored + sum(task.result() for task in dispatched if not task.cancelled())


async def _inject_lastwords_message(session: dict, snap: dict) -> bool:
    """notify：把 ``[lastwords]`` 折叠带追加进 session 历史（不跑 turn）。

    消息带 ``ginno_task_event`` 戳（task/lastwords）——messages_ui 投影折成
    task_event block，重放与 live 同折不露长原文。
    """
    from langchain_core.messages import HumanMessage

    sid = session["session_id"]
    text = render_band(snap)
    config = {
        "configurable": {
            "thread_id": sid,
            "project_slug": session["project_slug"],
            "turn_id": f"lastwords-{snap.get('turn_id') or 'x'}",
        }
    }
    message_id = f"lastwords-{snap['recovery_id']}"
    if any(getattr(msg, "id", None) == message_id for msg in _load_thread_messages(session["project_slug"], sid)):
        return True
    await session["graph"].aupdate_state(
        config,
        {
            "messages": [
                HumanMessage(
                    content=text,
                    id=message_id,
                    additional_kwargs={
                        "ginno_task_event": {
                            "task": f"lastwords-{sid[:8]}",
                            "kind": "lastwords",
                            "title": str(snap.get("user_request_head") or "")[:60],
                            "reason": str(snap.get("reason") or ""),
                        },
                        "ginno_lastwords": {"session_id": sid, "reason": snap.get("reason")},
                    },
                )
            ]
        },
        as_node="agent",
    )
    return True


async def _run_lastwords_turn(session: dict, snap: dict) -> str:
    """auto：headless 续跑 turn（镜像 sessions._run_goal_turn / inject wake）。"""
    from . import server_shared as shared
    from .api.stream import _run_stream

    shared.clear_inherited_llm_context()
    sid = session["session_id"]
    turn_id = snap.get("resume_turn_id") or f"lw-{uuid.uuid4().hex}"
    agent_id = session.get("agent_id")
    if not agent_id:
        from .api.sessions import _first_agent_id

        agent_id = _first_agent_id()
    if not agent_id:
        _log.info("lastwords_auto_dropped_no_agent session=%s", sid)
        return "failed"
    from .api.stream.remote_turn import remote_peer_of

    if remote_peer_of(session):
        # 远程会话恢复 = **重发最后用户请求原文到远端**（run_remote_turn
        # 经 referenceTaskIds 让对端 resume 停在 input-required 的任务）。
        # 绝不用 lastwords 模板文本：远端没有经历「重启」，模板里的
        # 「从中断处继续」指示对它没有上下文意义，反而误导。
        text = str(snap.get("user_request_head") or "").strip() or render_continuation(snap)
    else:
        text = render_continuation(snap)
    config = {
        "configurable": {
            "thread_id": sid,
            "project_slug": session["project_slug"],
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            # 用量归因（设计 §4）：遗言续跑在用量面板可辨识。
            "usage_source": "lastwords",
            # echo_user（engine.py turn.start tap 同款）：调度器 wake turn 靠
            # 这个标记让注入信封实时落输入气泡（runner.py 先例）。遗言 auto
            # 续跑此前没设——开着会话页时用户只看到 assistant 流，「续跑」
            # 指令文本和遗言带全靠 history 补，体感「遗言没渲染」
            # （2026-11-19 0897eeeb 实录）。设上后 turn.start 帧带 text，
            # live 立即出用户气泡；task_event 遗言带仍由 turn 结束后的
            # history reconcile 折入（与 subagent wake 同口径）。
            "echo_user": True,
        }
    }
    task = asyncio.current_task()
    stop = shared._TURN_STOP.setdefault(sid, asyncio.Event())
    shared._TURN_TASKS[sid] = task
    shared._RUNNING_TURNS[sid] = turn_id
    shared._RUNNING_TURNS_AT[sid] = time.time()
    # 2026-11-21 用户决策：遗言 auto 续跑不再标记无人值守。热切换/重启恢复
    # 的 turn 大多发生在用户眼前的会话页——ask_user 正常弹卡片等人（parked
    # turn 可恢复），比「自选默认 + 假设声明」更符合「我在看着它重启」的
    # 直觉。真正无人守的通道（schedule 注入 / A2A 入站 / 无头 CLI）不变。
    try:
        if snap.get("status") == "running":
            await _publish_receipt(snap)
        return await _run_stream(
            None,
            session["graph"],
            config,
            text,
            session,
            agent_id,
            user_extra_kwargs={
                "ginno_task_event": {
                    "task": f"lastwords-{sid[:8]}",
                    "kind": "lastwords",
                    "title": str(snap.get("user_request_head") or "")[:60],
                },
                "ginno_lastwords": {
                    "session_id": sid,
                    "reason": snap.get("reason"),
                    "recovery_id": snap.get("recovery_id"),
                    "request_head": snap.get("user_request_head", ""),
                },
            },
            origin="lastwords",
        )
    except asyncio.CancelledError:
        if stop.is_set():
            return "stopped"
        raise
    finally:
        if shared._TURN_TASKS.get(sid) is task:
            shared._TURN_TASKS.pop(sid, None)
        if shared._TURN_STOP.get(sid) is stop:
            shared._TURN_STOP.pop(sid, None)
        if shared._RUNNING_TURNS.get(sid) == turn_id and sid not in shared._PENDING_RESUME:
            shared._RUNNING_TURNS.pop(sid, None)
            shared._RUNNING_TURNS_AT.pop(sid, None)


# --------------------------------------------------------------------------- #
# SIGTERM 兜底诊断（2026-11-07 首跑实况复盘：reason=running 残留的根因）
# --------------------------------------------------------------------------- #
# 桌面壳（apps/desktop/src/sidecar.rs）有两条杀 sidecar 的路径：
#
# 1. 优雅退出（stop_child_gracefully，Cmd+Q/重启 runtime）：SIGTERM →
#    最多等 6s。uvicorn timeout_graceful_shutdown=3：等 in-flight 3s →
#    取消剩余任务 → **继续走 lifespan shutdown**（write_lastwords(signal)
#    在此执行）→ asyncio loop teardown 取消残余 turn 协程 → engine
#    finally 的 note_turn_end。两个坑：
#    a) loop teardown 会把刚写好的 signal 遗言**删掉**——已由
#       mark_shutdown()（server.py lifespan finally 首段置位）修复：
#       关闭中的 note_turn_end 不再删遗言；
#    b) turn 协程被取消时 checkpoint 已含本 turn 全部进展，遗言注入
#       与 checkpoint 恢复语义重叠，可接受。
# 2. **退出后立即重启（kill_stale_sidecar）**：SIGTERM 后仅 ~100ms
#    就对端口未释放的进程升级 SIGKILL（escalate 在等待循环第一个
#    tick 就触发），而 uvicorn 优雅关闭全程需要 ≥3s——lifespan finally
#    的 write_lastwords(signal) **结构性来不及执行**，这是首跑实况
#    reason=running 残留的根因。属壳的有意设计（防端口被僵死进程占
#    死、保启动速度），Python 侧无法补救。
#
# 结论：**reason=running 的 WAL 遗言是重启场景（尤其退出后立即重开）
# 的唯一防线**；consume 侧的低价值门槛过滤与 15min TTL 是对 WAL
# 「无差别快照」的补偿闸。若未来要堵死 2)，需放宽 kill_stale_sidecar
# 的 escalate 窗口到 ≥3.5s（Rust 侧改动，影响启动时序，另行评估）。
