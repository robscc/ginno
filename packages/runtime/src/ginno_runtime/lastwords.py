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
exactly-once：注入成功/session 不存在/异常终态/TTL 过期 → 删文件。

快照不调 LLM（重启窗口宝贵）；注入走 tasks/inject 的 ``[task …]`` 首行
族格式（``[lastwords] …`` 前缀），前端 foldTaskEventBlocks 家族识别折叠。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from . import paths

_log = logging.getLogger("ginno.lastwords")

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
# notify：人工会话挂起待确认（点「恢复」续跑）；subagent 无感直跑。
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
        for k in ("ginno_task_event", "ginno_steer", "ginno_lastwords", "ginno_subagent_result")
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
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


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
        try:
            msgs = loader(slug, sid)
            snap = snapshot_from_messages(
                sid, slug, msgs, reason=reason, fingerprint_old=fingerprint_old
            )
            snap["turn_id"] = turn_id
            # 出站远程 turn 覆盖（2026-11-18）：本地 checkpoint 没有在途
            # 请求（远端会话只在 turn 结束时落盘），快照必须取自执行器
            # 注册表里的真实 user_text / 流式尾部，否则抓到的是上一轮。
            reg = _REMOTE_HEADS.get(sid)
            if reg is not None and reg.get("turn_id") == turn_id:
                head = str(reg.get("user_text") or "").strip()
                if head:
                    snap["user_request_head"] = head[:HEAD_CHARS]
                tail = str(reg.get("agent_tail") or "").strip()
                if tail:
                    snap["last_agent_message"] = tail[-HEAD_CHARS:]
                snap["remote"] = True
                if reg.get("peer"):
                    snap["peer"] = str(reg["peer"])
                if reg.get("last_tool"):
                    snap["running_tools"] = [str(reg["last_tool"])]
            # 工作量信号（首跑质量修）：进程内计数随快照落盘——消费侧
            # 门槛判定需要（dev-swap/signal 遗言同样要带）。
            if sid in _EFFORT:
                snap["effort"] = dict(_EFFORT[sid])
            _write_atomic(_path(sid), snap)
            _LAST_WRITE_AT[sid] = time.monotonic()
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


def _reset_effort(session_id: str, turn_id: str) -> dict:
    e = {"turn_id": turn_id, "started_at": time.time(), "tool_calls": 0}
    _EFFORT[session_id] = e
    return e


def _bump_effort(session_id: str, turn_id: str) -> dict:
    """工具边界计数 +1（节流之外每次都记——节流只限写盘频率，不限计数）。"""
    e = _EFFORT.get(session_id)
    if e is None or e.get("turn_id") != turn_id:
        e = _reset_effort(session_id, turn_id)
    e["tool_calls"] = int(e.get("tool_calls") or 0) + 1
    return e


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
    if session_id in _EFFORT:
        snap["effort"] = dict(_EFFORT[session_id])
    _write_atomic(_path(session_id), snap)
    _LAST_WRITE_AT[session_id] = time.monotonic()


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
        _REMOTE_HEADS[session_id] = {
            "turn_id": turn_id,
            "user_text": user_text,
            "peer": peer,
            "agent_tail": "",
            "last_tool": "",
            "project_slug": project_slug,
        }
        snap_effort = _reset_effort(session_id, turn_id)
        _write_remote_snapshot(session_id, REASON_RUNNING)
        _log.info(
            "lastwords_remote_turn_start session=%s turn=%s peer=%s effort=%s",
            session_id, turn_id, peer, snap_effort.get("tool_calls"),
        )
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
    reg = _REMOTE_HEADS.get(session_id)
    if reg is None or reg.get("turn_id") != turn_id:
        return
    try:
        if agent_tail is not None:
            reg["agent_tail"] = (str(reg.get("agent_tail") or "") + str(agent_tail))[-2000:]
        if tool_name:
            _bump_effort(session_id, turn_id)
            reg["last_tool"] = str(tool_name)
            now = time.monotonic()
            last = _LAST_WRITE_AT.get(session_id)
            if last is None or (now - last) >= TOOL_WRITE_MIN_INTERVAL_S:
                _write_remote_snapshot(session_id, REASON_RUNNING)
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_remote_progress_failed session=%s", session_id)


def note_remote_turn_end(session_id: str, turn_id: str) -> None:
    """远端 turn 收口：注销注册表 + 走 note_turn_end 语义（删遗言）。

    必须挂在执行体 finally（协程被取消也要清）；优雅关闭中不删文件
    （``_SHUTTING_DOWN``，与本地版同款 SIGTERM 保护）。
    """
    reg = _REMOTE_HEADS.pop(session_id, None)
    if reg is not None and reg.get("turn_id") != turn_id:
        # 后继 turn 已重注册：把注册表还回去，只按身份校验跳过清理
        _REMOTE_HEADS[session_id] = reg
        return
    note_turn_end(session_id, turn_id)


def note_turn_start(session_id: str, turn_id: str, project_slug: str | None) -> None:
    """turn 启动即写初始遗言（reason=running）——工具执行期被 SIGKILL 也有底。"""
    if not session_id:
        return
    try:
        slug = project_slug or _resolve_slug(session_id)
        if not slug:
            return
        msgs = _load_thread_messages(slug, session_id)
        snap = snapshot_from_messages(session_id, slug, msgs, reason=REASON_RUNNING)
        snap["turn_id"] = turn_id
        snap["effort"] = _reset_effort(session_id, turn_id)
        _write_atomic(_path(session_id), snap)
        _LAST_WRITE_AT[session_id] = time.monotonic()
    except Exception:  # noqa: BLE001 — 遗言绝不拖累 turn 本体
        _log.exception("lastwords_turn_start_failed session=%s", session_id)


def note_tool_boundary(
    session_id: str, turn_id: str, project_slug: str | None, messages: list[Any]
) -> None:
    """工具调用边界增量覆盖写（节流：同 session ≥TOOL_WRITE_MIN_INTERVAL_S）。"""
    if not session_id:
        return
    # 计数先行（节流前）——被节流丢掉的写盘不丢工作量信号。
    effort = _bump_effort(session_id, turn_id)
    now = time.monotonic()
    last = _LAST_WRITE_AT.get(session_id)
    if last is not None and (now - last) < TOOL_WRITE_MIN_INTERVAL_S:
        return
    try:
        slug = project_slug or _resolve_slug(session_id)
        if not slug:
            return
        snap = snapshot_from_messages(session_id, slug, messages, reason=REASON_RUNNING)
        snap["turn_id"] = turn_id
        snap["effort"] = dict(effort)
        _write_atomic(_path(session_id), snap)
        _LAST_WRITE_AT[session_id] = now
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_tool_boundary_failed session=%s", session_id)


def note_turn_end(session_id: str, turn_id: str) -> None:
    """turn 正常收尾：遗言作废即删——绝不把已完成的 turn 当恢复对象。

    interrupt 停车（PENDING_RESUME）也算段结束：parked turn 重启后由
    checkpoint resume 机制自己管，遗言注入反而多余。
    例外：进程优雅关闭中（lifespan shutdown 后 asyncio loop teardown
    cancel 残余 turn 协程也会走到这里）不删——刚写好的 signal 遗言
    正等着重启后的新进程消费，见模块尾「SIGTERM 兜底诊断」。
    """
    if not session_id:
        return
    _LAST_WRITE_AT.pop(session_id, None)
    _EFFORT.pop(session_id, None)
    if _SHUTTING_DOWN:
        return
    try:
        p = _path(session_id)
        if p.exists():
            p.unlink()
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


def consume_lastwords(*, now: float | None = None) -> list[dict]:
    """扫描遗言目录：TTL 过期/低价值即删；返回有效条目（不删——恢复流程处置）。"""
    now = time.time() if now is None else now
    out: list[dict] = []
    d = lastwords_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            snap = json.loads(p.read_text())
        except Exception:  # noqa: BLE001 — 损坏文件（写一半撞 SIGKILL 等）
            _log.warning("lastwords_corrupt_dropped file=%s", p.name)
            _drop(p)
            continue
        if p.name == _MARKER_NAME:
            # 空标记：无 session 可注入，留痕即删。
            _log.info("lastwords_marker_consumed reason=%s", snap.get("reason"))
            _drop(p)
            continue
        ts = snap.get("ts") or 0
        ttl = RUNNING_TTL_S if str(snap.get("reason")) == REASON_RUNNING else TTL_S
        if now - ts > ttl:
            _log.info(
                "lastwords_expired_dropped session=%s age=%.0fs",
                snap.get("session_id"),
                now - ts,
            )
            _drop(p)
            continue
        if not snap.get("session_id"):
            _drop(p)
            continue
        if not _worth_restoring(snap):
            eff = snap.get("effort") or {}
            _log.info(
                "lastwords_low_effort_dropped session=%s reason=%s tool_calls=%s elapsed=%.0fs",
                snap.get("session_id"),
                snap.get("reason"),
                eff.get("tool_calls"),
                float(snap.get("ts") or 0) - float(eff.get("started_at") or 0),
            )
            _drop(p)
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



def pending_resume_snapshot(session_id: str) -> dict | None:
    """待确认续跑条目（过期视为无）。"""
    snap = _PENDING_RESUME.get(session_id)
    if not snap:
        return None
    if time.time() - float(snap.get("pending_at") or 0) > TTL_S:
        _PENDING_RESUME.pop(session_id, None)
        drop_lastwords(session_id)
        return None
    return snap


def list_pending(*, now: float | None = None) -> list[dict]:
    """列出待确认续跑条目（2026-11-16 新增，供前端**拉取式**重建决策卡）。

    为什么必须拉取而不能只靠广播：``_push_global_event`` 只遍历**当前活着
    的 socket**（server_shared.py），无缓冲无重放；而 recover 在 lifespan
    +2s 就广播，前端 WS 重连间隔 3s——**真实帧经常先于 socket 就绪发出，
    静默丢失**。这正是用户「遗言好像没触发过」的最后一环（前两环是
    notify 无交互 + pending 条件太窄，已修）。packaged e2e 作者实测撞上
    此竞态，不得不按契约补投一帧才测得下去。

    拉取式另修一个缺口：收件箱条目的 action 回调不可序列化，应用重启后
    只剩「跳会话」，决策按钮丢失；有了本端点，前端连上即可重建。

    过期的顺手清掉（与 pending_resume_snapshot 同口径）。
    """
    now = time.time() if now is None else now
    out: list[dict] = []
    for sid in list(_PENDING_RESUME.keys()):
        snap = _PENDING_RESUME.get(sid) or {}
        if now - float(snap.get("pending_at") or 0) > TTL_S:
            _PENDING_RESUME.pop(sid, None)
            drop_lastwords(sid)
            continue
        eff = snap.get("effort") or {}
        out.append({
            "session_id": sid,
            "head": str(snap.get("user_request_head") or "")[:160],
            "user_request_head": str(snap.get("user_request_head") or "")[:160],
            "reason": str(snap.get("reason") or ""),
            "bg_running": len(snap.get("bg_tasks") or []),
            "bg_interrupted": len(snap.get("bg_interrupted") or []),
            "bg_interrupted_ids": list(snap.get("bg_interrupted") or []),
            "tool_calls": eff.get("tool_calls", 0),
            "elapsed_s": max(
                0.0,
                float(snap.get("ts") or 0) - float(eff.get("started_at") or 0),
            ),
            "pending_at": float(snap.get("pending_at") or 0),
        })
    return sorted(out, key=lambda d: d["pending_at"])


async def resume_pending(session_id: str) -> dict:
    """确认续跑（/api/lastwords/resume）。

    2026-11-17 修复「恢复失败：Load failed」：原实现在 HTTP 请求里同步
    await 整个续跑 turn（数分钟），响应迟迟不返回 → 浏览器 fetch 空闲超时
    抛 "Load failed"（后端 turn 其实可能在正常跑）。改为 spawn_bg 异步起
    turn、立即返回 ``{ok:true}``（=已发起续跑），与 auto 路径 headless 续跑
    同范式；终态清理 / 广播在后台任务里完成。
    """
    snap = pending_resume_snapshot(session_id)
    if not snap:
        return {"ok": False, "error": "no_pending"}
    _PENDING_RESUME.pop(session_id, None)
    from . import server_shared as shared
    from .api.sessions import _ensure_session

    session = _ensure_session(session_id)

    async def _resume_bg() -> None:
        ok = False
        try:
            ok = await _run_lastwords_turn(session, snap)
        except Exception:  # noqa: BLE001
            _log.exception("lastwords_resume_failed session=%s", session_id)
        drop_lastwords(session_id)
        if ok:
            _log.info("lastwords_restored session=%s mode=auto-confirmed reason=%s",
                      session_id, snap.get("reason"))
            try:
                await shared._push_global_event(
                    "runtime.lastwords_restored",
                    {"session_id": session_id, "mode": "auto-confirmed"},
                )
            except Exception:  # noqa: BLE001
                pass

    shared.spawn_bg(_resume_bg())
    return {"ok": True}


def dismiss_pending(session_id: str) -> dict:
    """用户放弃续跑：清 pending + 删遗言文件。"""
    had = _PENDING_RESUME.pop(session_id, None) is not None
    drop_lastwords(session_id)
    if had:
        _log.info("lastwords_pending_dismissed session=%s", session_id)
    return {"ok": True, "had": had}


async def recover_lastwords() -> int:
    """启动扫描 → 按 mode 注入 → exactly-once 删文件。

    notify（默认）/signal·running 崩溃遗言：把 ``[lastwords]`` 带作为
    HumanMessage 追加进 session 历史（aupdate_state，compaction 同款先例）
    ——不跑 turn 不烧 token，agent 下次被唤醒时可见，重放折叠不露长原文。
    auto 且（reason=dev-swap **或 subagent 型会话**）：另起 headless 续跑
    turn（scheduler 同款安全）——subagent 本就 headless，自动续跑零打扰
    （2026-11-07 首跑追加：app 重启杀掉的子会话无人点「继续」，auto 放宽
    到 signal/running）。
    任何终态（成功/session 不存在/异常）都删文件——TTL 之外的第二道
    exactly-once 闸。
    """
    entries = consume_lastwords()
    if not entries:
        return 0
    from . import server_shared as shared

    m = mode()
    restored = 0
    for snap in entries:
        sid = str(snap.get("session_id"))
        reason = str(snap.get("reason") or "")
        try:
            from .api.sessions import _ensure_session

            session = _ensure_session(sid)
        except Exception:  # noqa: BLE001
            _log.exception("lastwords_session_lookup_failed session=%s", sid)
            session = None
        if session is None:
            _log.info("lastwords_dropped_no_session session=%s", sid)
            drop_lastwords(sid)
            continue
        if m == "off":
            _log.info("lastwords_off_dropped session=%s", sid)
            drop_lastwords(sid)
            continue
        # 注入前实时核对 bg 任务状态（首跑质量修）：快照过时的已完成任务
        # 从「可收割」提示中移除，避免误导 agent/用户。
        try:
            reconcile_bg_tasks(snap)
        except Exception:  # noqa: BLE001 — 核对绝不拦注入
            _log.exception("lastwords_bg_reconcile_failed session=%s", sid)
        ok = False
        how = "notify"
        try:
            # auto 分级（§4 + 首跑追加）：subagent 会话本就 headless——自动
            # 续跑零打扰，reason 放宽到 dev-swap/signal/running；人工会话
            # 维持仅 dev-swap（有真人在看，自动续跑反而打扰 + 崩溃/信号
            # 场景自动烧 token 的既有安全分级不破）。
            # 2026-11-10 产品化：人工会话的 auto 不再直接续跑——注入带 +
            # 挂起待确认（广播 runtime.lastwords_pending，用户点通知进会话
            # 后经 /api/lastwords/resume 一键续跑）；subagent 保持无感直跑。
            # 2026-11-18 出站远程会话（remote_peer）：**自动续跑一律拦下**，
            # 降级为挂起待确认。本地重启不影响远端进程——对端任务很可能
            # 仍在跑或已完成，无脑重发用户请求 = 重复派活/并发冲突；只有
            # 用户经 resume 显式确认后才重发（_run_lastwords_turn 对远端
            # 会话重发最后用户请求原文，远端凭自己的 checkpoint 续跑）。
            from .api.stream.remote_turn import remote_peer_of

            is_remote = bool(remote_peer_of(session or {}))
            if is_remote:
                ok = await _inject_lastwords_message(session, snap)
                how = "notify"
                if ok:
                    _PENDING_RESUME[sid] = {**snap, "pending_at": time.time()}
                    try:
                        eff = snap.get("effort") or {}
                        await shared._push_global_event(
                            "runtime.lastwords_pending",
                            {
                                "session_id": sid,
                                "head": str(snap.get("user_request_head") or "")[:160],
                                "user_request_head": str(
                                    snap.get("user_request_head") or ""
                                )[:160],
                                "reason": reason,
                                "remote": True,
                                "tool_calls": eff.get("tool_calls", 0),
                                "elapsed_s": max(
                                    0.0,
                                    float(snap.get("ts") or 0)
                                    - float(eff.get("started_at") or 0),
                                ),
                            },
                        )
                    except Exception:  # noqa: BLE001 — 广播失败不拦注入
                        _log.exception("lastwords_pending_broadcast_failed session=%s", sid)
                    _log.info(
                        "lastwords_remote_pending_confirm session=%s reason=%s", sid, reason
                    )
                    continue  # 文件保留——resume/dismiss 时才处置
            elif is_subagent_session(session):
                if m == "auto":
                    # subagent 本就 headless：auto 下无感直跑（reason 不限，
                    # 2026-11-07 首跑语义）。
                    ok = await _run_lastwords_turn(session, snap)
                    how = "auto"
                else:
                    ok = await _inject_lastwords_message(session, snap)
                    how = "notify"
            elif m == "auto":
                # 人工会话 auto=**真自动续跑**（2026-11-19 恢复语义，用户
                # 配置化：settings.json → devswap.lastwords.mode="auto"，
                # 无 UI）。2026-11-10/16 曾把人工会话的 auto 收敛为挂起
                # 待确认（防崩溃后自动烧 token 打扰真人）；但 auto 因此与
                # notify 完全等价成为死配置。现在恢复分级：
                #   notify（默认）= 挂起待确认，点「恢复」才续跑；
                #   auto = 重启/崩溃后人工会话也无感自动续跑（subagent
                #          路径同款 _run_lastwords_turn，质量门槛/TTL/
                #          exactly-once/remote 拦截全部沿用）。
                # 安全边界不破：默认仍是 notify；选 auto 即用户显式接受
                # 「重启后自动接着干烧 token」；remote 会话在上面已拦。
                ok = await _run_lastwords_turn(session, snap)
                how = "auto"
            elif m == "notify":
                # 人工会话：**任何 reason** 都挂起待确认（2026-11-16 放宽）。
                # 原条件 `m == "auto" and reason == REASON_DEV_SWAP` 太窄：
                # 重启/崩溃（reason=running/signal）拿不到任何交互提示，
                # 而默认 mode 就是 notify → 用户只看到静默注入的带 + 一条
                # 被动收件箱条目，实测「遗言好像没触发过」。
                # **安全分级不破**：这里只挂起 + 广播，绝不自动跑 turn；
                # 续跑必须用户点「恢复」经 /api/lastwords/resume。崩溃场景
                # 不自动烧 token 的既有约束因此仍然成立。
                # off 已在上面早退，不会走到这里。
                ok = await _inject_lastwords_message(session, snap)
                how = "notify"
                if ok:
                    _PENDING_RESUME[sid] = {**snap, "pending_at": time.time()}
                    try:
                        eff = snap.get("effort") or {}
                        await shared._push_global_event(
                            "runtime.lastwords_pending",
                            {
                                "session_id": sid,
                                # 快照真字段是 user_request_head；原实现取
                                # snap["head"]（从不存在）→ 通知摘要恒空。
                                # 两个键都发：head 兼容既有前端读法。
                                "head": str(snap.get("user_request_head") or "")[:160],
                                "user_request_head": str(
                                    snap.get("user_request_head") or ""
                                )[:160],
                                "reason": reason,
                                "bg_running": len(snap.get("bg_tasks") or []),
                                "bg_interrupted": len(snap.get("bg_interrupted") or []),
                                "bg_interrupted_ids": list(snap.get("bg_interrupted") or []),
                                "tool_calls": eff.get("tool_calls", 0),
                                "elapsed_s": max(
                                    0.0,
                                    float(snap.get("ts") or 0)
                                    - float(eff.get("started_at") or 0),
                                ),
                            },
                        )
                    except Exception:  # noqa: BLE001 — 广播失败不拦注入
                        _log.exception("lastwords_pending_broadcast_failed session=%s", sid)
                    _log.info(
                        "lastwords_pending_confirm session=%s reason=%s", sid, reason
                    )
                    continue  # 文件保留——resume/dismiss 时才处置
            else:
                ok = await _inject_lastwords_message(session, snap)
                how = "notify"
        except Exception:  # noqa: BLE001 — 恢复失败不炸启动
            _log.exception("lastwords_recover_failed session=%s", sid)
        drop_lastwords(sid)
        if ok:
            restored += 1
            _log.info("lastwords_restored session=%s mode=%s reason=%s", sid, how, reason)
            # 通知中心待恢复列表（设计 P2）：经全局 WS 广播，前端 pushNotice
            # 进收件箱（kind:lastwords，dedupeKey: lastwords:<session>）。
            try:
                await shared._push_global_event(
                    "runtime.lastwords_restored",
                    {
                        "session_id": sid,
                        "reason": reason,
                        "mode": m if m != "off" else "notify",
                        "band": render_band(snap),
                    },
                )
            except Exception:  # noqa: BLE001
                pass
    return restored


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
    await session["graph"].aupdate_state(
        config,
        {
            "messages": [
                HumanMessage(
                    content=text,
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


async def _run_lastwords_turn(session: dict, snap: dict) -> bool:
    """auto：headless 续跑 turn（镜像 sessions._run_goal_turn / inject wake）。"""
    from . import server_shared as shared
    from .api.stream import _run_stream
    from .tools.ask_tools import reset_interactive, set_interactive

    shared.clear_inherited_llm_context()
    sid = session["session_id"]
    turn_id = f"lw{int(time.time())}-{sid[:6]}"
    agent_id = session.get("agent_id")
    if not agent_id:
        from .api.sessions import _first_agent_id

        agent_id = _first_agent_id()
    if not agent_id:
        _log.info("lastwords_auto_dropped_no_agent session=%s", sid)
        return False
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
        }
    }
    _RUNNING = shared._RUNNING_TURNS
    _RUNNING[sid] = turn_id
    ask_tok = set_interactive(False)
    try:
        await _run_stream(
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
                "ginno_lastwords": {"session_id": sid, "reason": snap.get("reason")},
            },
            origin="lastwords",
        )
    finally:
        reset_interactive(ask_tok)
        if _RUNNING.get(sid) == turn_id and sid not in shared._PENDING_RESUME:
            _RUNNING.pop(sid, None)
    return True


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
