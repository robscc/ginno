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
# 过期文件在启动扫描时直接清理。
TTL_S = 3600.0

# reason 枚举（设计 §2）：dev-swap | signal | running（P2 WAL）。
REASON_DEV_SWAP = "dev-swap"
REASON_SIGNAL = "signal"
REASON_RUNNING = "running"

# settings.json → devswap.lastwords.mode 的合法值（默认 notify）。
MODES = ("notify", "auto", "off")
DEFAULT_MODE = "notify"

# 快照正文的截断长度（设计 §2：前 500 字）。
HEAD_CHARS = 500

# P2 WAL 工具边界写的节流间隔：同 session 两次写至少间隔这么久
# （turn 启动那次不受限——首轮必须立刻有遗言护体）。
TOOL_WRITE_MIN_INTERVAL_S = 5.0

# 无活跃 turn 时的空标记文件名。下划线开头保证不与 session_id（hex）
# 冲突；消费侧看到即删（无 session 可注入，只为审计留痕）。
_MARKER_NAME = "_marker.json"

# 每 session 的上次 WAL 写时间（节流状态，进程内）。
_LAST_WRITE_AT: dict[str, float] = {}


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
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(x for x in parts if x)
    return str(content or "")


def _is_injected(m: Any) -> bool:
    """注入消息（task/steer/lastwords/子代理结果带）不作为 user_request 候选。"""
    kw = getattr(m, "additional_kwargs", None) or {}
    return any(
        k in kw for k in ("ginno_task_event", "ginno_steer", "ginno_lastwords", "ginno_subagent_result")
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

        tup = FileCheckpointer(project_slug).get_tuple(
            {"configurable": {"thread_id": session_id}}
        )
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
            _write_atomic(_path(sid), snap)
            _LAST_WRITE_AT[sid] = time.monotonic()
            count += 1
        except Exception:  # noqa: BLE001 — 单 session 失败不拖累其余
            _log.exception("lastwords_write_failed session=%s", sid)
    if not count and reason in (REASON_DEV_SWAP, REASON_SIGNAL):
        try:
            _write_atomic(
                lastwords_dir() / _MARKER_NAME,
                {"reason": reason, "sessions": [], "fingerprint_old": fingerprint_old,
                 "ts": int(time.time())},
            )
        except Exception:  # noqa: BLE001
            _log.exception("lastwords_marker_write_failed")
    if count or reason in (REASON_DEV_SWAP, REASON_SIGNAL):
        _log.info(
            "lastwords_written reason=%s count=%d sessions=%s",
            reason, count, ",".join(sorted(live)) or "-",
        )
    return count


# --------------------------------------------------------------------------- #
# P2 WAL：turn 生命周期钩子（engine._stream_graph / graph.tools_node 调用）
# --------------------------------------------------------------------------- #

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
        _write_atomic(_path(session_id), snap)
        _LAST_WRITE_AT[session_id] = now
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_tool_boundary_failed session=%s", session_id)


def note_turn_end(session_id: str, turn_id: str) -> None:
    """turn 正常收尾：遗言作废即删——绝不把已完成的 turn 当恢复对象。

    interrupt 停车（PENDING_RESUME）也算段结束：parked turn 重启后由
    checkpoint resume 机制自己管，遗言注入反而多余。
    """
    if not session_id:
        return
    _LAST_WRITE_AT.pop(session_id, None)
    try:
        p = _path(session_id)
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        _log.exception("lastwords_turn_end_failed session=%s", session_id)


# --------------------------------------------------------------------------- #
# 消费（启动扫描）
# --------------------------------------------------------------------------- #

def consume_lastwords(*, now: float | None = None) -> list[dict]:
    """扫描遗言目录：TTL 过期即删；返回有效条目（不删——恢复流程处置）。"""
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
        if now - ts > TTL_S:
            _log.info(
                "lastwords_expired_dropped session=%s age=%.0fs",
                snap.get("session_id"), now - ts,
            )
            _drop(p)
            continue
        if not snap.get("session_id"):
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


def render_band(snap: dict) -> str:
    """注入文本首行（``[task finished]`` 同族格式，前端折叠识别）。"""
    head = str(snap.get("user_request_head") or "").strip().replace("\n", " ")
    summary = head[:80] + ("…" if len(head) > 80 else "")
    reason = str(snap.get("reason") or "")
    fp = str(snap.get("fingerprint_old") or "")
    fp_note = f" · 旧指纹 {fp[:12]}" if fp else ""
    bg = snap.get("bg_tasks") or []
    bg_note = f" · 后台任务 {len(bg)} 个可收割(bg_logs)" if bg else ""
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
        lines.append(f"后台任务 {', '.join(map(str, bg))} 可能仍在跑或已结束，用 bg_logs 收割结果。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 恢复流程（server.py lifespan 启动段后台 task 调用）
# --------------------------------------------------------------------------- #

async def recover_lastwords() -> int:
    """启动扫描 → 按 mode 注入 → exactly-once 删文件。

    notify（默认）/signal·running 崩溃遗言：把 ``[lastwords]`` 带作为
    HumanMessage 追加进 session 历史（aupdate_state，compaction 同款先例）
    ——不跑 turn 不烧 token，agent 下次被唤醒时可见，重放折叠不露长原文。
    auto 且 reason=dev-swap：另起 headless 续跑 turn（scheduler 同款安全）。
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
        ok = False
        how = "notify"
        try:
            if m == "auto" and reason == REASON_DEV_SWAP:
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
            None, session["graph"], config, text, session, agent_id,
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
