"""任务事件注入（background-tasks-design.md §2 D2/D6、§5）。

完成 / 停止 / 超时 / healthy / watch 事件回到发起 session 的双路：

- session 在跑 → steer 入队（``origin="task"``，持久化消息带
  ``ginno_task_event`` 戳——messages_ui 据此渲染 task 带，不落用户气泡）；
  段末未被吸收的条目由 ``steer_keep_channel`` 的扩展保留，本模块的补投
  watcher 在 turn 收尾后捞回、以 wake turn 重投（channels adapter 同款
  「已追加不是空头支票」语义）。
- session 空闲 → 5s 合并窗（D6）后一个 headless wake turn（模式镜像
  subagent_scheduler 的 _inject_result/_flush_injections 与 sessions 的
  _run_goal_turn：早挂 _RUNNING_TURNS、headless 拒 ask_user、收尾清旗）。

注入是模型侧输入；UI 不渲染成第二条消息（评审决策 4）——前端按
task_id 折进常驻卡片。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .. import server_shared as shared
from . import store

_log = logging.getLogger("ginno.tasks")

# D6 合并窗：同 session 短窗内多个任务事件合成一个 wake turn。
COALESCE_S = 5.0

# session_id -> 待注入事件列表（idle 路径合并窗）
_PENDING: dict[str, list[dict]] = {}
# session_id -> 合并窗计时 asyncio.Task（已在等 flush 的不再重复起）
_FLUSH_TASKS: dict[str, object] = {}
# 补投 watcher：steer 入队后等吸收，超时未吸收则捞回重投
_STEER_WATCHERS: dict[str, object] = {}
# steer 注入后最多等这么久仍未吸收（turn 卡在长工具调用）就放弃补投——
# 事件文本还在，模型 bg_list 也能看到；避免无限 watcher 堆积。
_STEER_RESCUE_S = 600.0


def _session_live(session_id: str) -> bool:
    task = shared._TURN_TASKS.get(session_id)
    return session_id in shared._RUNNING_TURNS or (task is not None and not task.done())


def on_task_event(record: dict, kind: str, *, line: str = "", label: str = "") -> None:
    """reaper（asyncio 上下文）调用：渲染事件文本并投递。"""
    session_id = record.get("origin_session")
    if not session_id:
        return
    text = render_event(record, kind, line=line, label=label)
    payload = {
        "task": record["id"],
        "kind": kind,
        "title": record.get("title") or record.get("command", "")[:40],
        "exit": record.get("exit_code"),
        "dur": record.get("dur") or "",
    }
    try:
        from ..api.sessions import _ensure_session  # lazy: cycle

        if _ensure_session(session_id) is None:
            _log.info("task_event_dropped_no_session task=%s session=%s", record["id"], session_id)
            return
    except Exception:  # noqa: BLE001
        _log.exception("task_event_session_lookup_failed task=%s", record["id"])
        return
    if _session_live(session_id):
        _steer_inject(session_id, text, payload)
    else:
        _queue_injection(session_id, text, payload)


# --------------------------------------------------------------------------- #
# 事件文本模板（§5）
# --------------------------------------------------------------------------- #

def render_event(record: dict, kind: str, *, line: str = "", label: str = "") -> str:
    tid = record["id"]
    title = record.get("title") or record["command"][:40]
    if kind == "healthy":
        ran = _fmt_since(record.get("started_at"))
        return f'[task healthy] "{title}" ({tid}) ready after {ran} — matched "{label or line.strip()[:60]}"'
    if kind == "watch":
        shown = line.strip()[:300]
        return f'[task watch] "{title}" ({tid}) matched "{label}": {shown}'
    if kind == "watch_muted":
        return (
            f'[task watch muted] "{title}" ({tid}) matched "{label}" too many times; '
            f"further matches are silent. Use bg_logs to inspect."
        )
    head = {
        store.STATUS_FINISHED: f'[task finished] "{title}" ({tid}) exit {record.get("exit_code")} · ran {record.get("dur")}',
        store.STATUS_STOPPED: f'[task stopped by user] "{title}" ({tid}) after {record.get("dur")}',
        store.STATUS_TIMEOUT: f'[task timeout] "{title}" ({tid}) after {record.get("dur")} — killed',
        store.STATUS_INTERRUPTED: f'[task interrupted by app quit] "{title}" ({tid})',
        store.STATUS_FAILED_TO_START: f'[task failed to start] "{title}" ({tid}) {record.get("exit_note", "")}',
    }.get(kind)
    if head is None:
        head = f"[task {kind}] \"{title}\" ({tid})"
    tail = store.read_log_tail(tid, int(store.config()["inject_tail_lines"]))
    body = "\n".join(tail)
    return (
        f"{head}\n--- tail ---\n{body}\n--- end ---\n"
        f"Full log: {store.log_path(tid)}"
    )


def _fmt_since(ts: float | None) -> str:
    if not ts:
        return "0s"
    s = max(0, int(time.time() - ts))
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


# --------------------------------------------------------------------------- #
# busy 路径：steer + 补投
# --------------------------------------------------------------------------- #

def _steer_inject(session_id: str, text: str, payload: dict) -> None:
    steer_id = f"task-{payload['task']}-{uuid.uuid4().hex[:8]}"
    shared.steer_enqueue(
        session_id,
        {
            "steer_id": steer_id,
            "turn_id": shared._RUNNING_TURNS.get(session_id) or "",
            "text": text,
            "injected_at": time.time(),
            # 与 channel 来源同待遇：段末未吸收不清场（steer_keep_channel
            # 同时保留 origin="task"），由补投 watcher 捞回。
            "origin": "task",
            "extra_kwargs": {"ginno_task_event": payload},
        },
    )
    _log.info("task_event_steer session=%s task=%s", session_id, payload["task"])
    # 补投 watcher：等 turn 收尾后检查吸收情况。
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # 非 asyncio 上下文（不该发生）：放弃补投，steer 仍会被活 turn 吸收
    if session_id in _STEER_WATCHERS:
        return
    _STEER_WATCHERS[session_id] = loop.create_task(_rescue_steer(session_id, steer_id))


async def _rescue_steer(session_id: str, steer_id: str) -> None:
    try:
        deadline = time.time() + _STEER_RESCUE_S
        while time.time() < deadline:
            await asyncio.sleep(1.0)
            stash = shared._STEER_STASH.get(session_id) or []
            if not any(e.get("steer_id") == steer_id for e in stash):
                return  # 已被吸收（或本就不在了）
            if not _session_live(session_id):
                # turn 收尾了但条目没被吸收：捞回，走 idle 注入重投。
                entry = next(e for e in stash if e.get("steer_id") == steer_id)
                shared._STEER_STASH[session_id] = [
                    e for e in stash if e.get("steer_id") != steer_id
                ]
                if not shared._STEER_STASH[session_id]:
                    shared._STEER_STASH.pop(session_id, None)
                payload = (entry.get("extra_kwargs") or {}).get("ginno_task_event") or {}
                _log.info("task_steer_rescued session=%s task=%s", session_id, payload.get("task"))
                _queue_injection(session_id, entry.get("text") or "", payload)
                return
    finally:
        _STEER_WATCHERS.pop(session_id, None)


# --------------------------------------------------------------------------- #
# idle 路径：合并窗 + wake turn
# --------------------------------------------------------------------------- #

def _queue_injection(session_id: str, text: str, payload: dict) -> None:
    _PENDING.setdefault(session_id, []).append({"text": text, "payload": payload})
    if session_id in _FLUSH_TASKS:
        return  # 已有合并窗在等，本条搭车
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _FLUSH_TASKS[session_id] = loop.create_task(_flush_after(session_id))


async def _flush_after(session_id: str) -> None:
    try:
        await asyncio.sleep(COALESCE_S)
    finally:
        _FLUSH_TASKS.pop(session_id, None)
    if _session_live(session_id):
        # 等窗期间 session 被叫活了：全部转 steer（活 turn 的下个 superstep 排水）。
        for e in _PENDING.pop(session_id, []):
            _steer_inject(session_id, e["text"], e["payload"])
        return
    entries = _PENDING.pop(session_id, [])
    if not entries:
        return
    if len(entries) == 1:
        text = entries[0]["text"]
        payload = entries[0]["payload"]
    else:
        text = (
            f"{len(entries)} background task events arrived:\n\n"
            + "\n\n".join(e["text"] for e in entries)
        )
        payload = entries[-1]["payload"]  # 带上最后一个事件的戳；文本里全量
    await _run_wake_turn(session_id, text, payload)


async def _run_wake_turn(session_id: str, text: str, payload: dict) -> None:
    """镜像 sessions._run_goal_turn / subagent runner 的 headless turn。"""
    from ..api.sessions import _ensure_session, _first_agent_id
    from ..api.stream import _run_stream
    from ..tools.ask_tools import reset_interactive, set_interactive

    shared.clear_inherited_llm_context()  # 不许继承 spawning turn 的 LLM 流上下文
    session = shared._SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        _log.info("task_wake_dropped_no_session session=%s", session_id)
        return
    turn_id = uuid.uuid4().hex
    agent_id = session.get("agent_id") or _first_agent_id()
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": session["project_slug"],
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": "task",
        }
    }
    _RUNNING = shared._RUNNING_TURNS
    _RUNNING[session_id] = turn_id
    # 无人值守：ask_user 拒绝作答而不是停车等人（goal turn 同款）。
    ask_tok = set_interactive(False)
    try:
        await _run_stream(
            None, session["graph"], config, text, session, agent_id,
            user_extra_kwargs={"ginno_task_event": payload},
        )
    finally:
        reset_interactive(ask_tok)
        if _RUNNING.get(session_id) == turn_id and session_id not in shared._PENDING_RESUME:
            _RUNNING.pop(session_id, None)
