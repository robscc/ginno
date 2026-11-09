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
    try:
        from .tasks import store as task_store

        idx = task_store.read_index()
        return sorted(
            tid
            for tid, rec in idx.items()
            if rec.get("origin_session") == session_id
            and rec.get("status") == task_store.STATUS_RUNNING
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
    if not bg:
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
    return f"[lastwords] {reason} 重启恢复：{summary}{fp_note}{bg_note}"


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
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 恢复流程（server.py lifespan 启动段后台 task 调用）
# --------------------------------------------------------------------------- #


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
            auto_ok = m == "auto" and (reason == REASON_DEV_SWAP or is_subagent_session(session))
            if auto_ok:
                ok = await _run_lastwords_turn(session, snap)
                how = "auto"
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
