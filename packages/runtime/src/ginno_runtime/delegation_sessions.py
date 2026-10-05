"""Delegation sub-sessions: replayable archives of external agent runs.

外部编码代理每次 ``delegate_agent`` 调用都会在这里落一个真正的子会话
（external-agents-design.md §9）：

* meta 走 session_meta 的磁盘索引（``type="delegation"`` +
  ``parent_session_id``），列表/级联删除沿用 subagent 的现成机制——
  ``_session_meta_descendants`` 只认 parent 链、不看类型；
* 转录走 FileCheckpointer：后端解析器产出的 IR（assistant / tool_result
  条目）转成 LangChain 消息写进 checkpoint，``GET /sessions/{id}/history``
  与聊天 UI 原样渲染——不需要专门的"回放器"；
* 原始事件流归档到 ``sessions/<id>/delegation-events.jsonl``（上限
  512KB），用于排查与将来重转换。

模块契约：``record_delegation`` 绝不抛异常，失败返回 ""（调用方仍套着
try/except 双保险）。
"""
from __future__ import annotations

import os
import shutil
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from . import paths
from .checkpointer import FileCheckpointer
from .session_meta import _find_meta, _session_meta_upsert, subagent_depth_of

_RAW_CAP = 512_000
# 存量上限:每 slug 只保留最近 N 条委托归档(2026-10-05 单日即产生 44 条,
# 不封顶会无限累积;running 态的永不清理)。
_MAX_DELEGATION_SESSIONS = 60
# 委托子会话图标按后端取:claude-code 用专属星芒标(前端 icons.tsx 同名
# 组件),其余后端沿用通用 terminal。
_DELEGATION_ICONS = {"claude-code": "claude-code", "codex": "codex", "pi": "pi"}


def _ir_to_messages(ir: list, prompt: str) -> list:
    """IR → LangChain 消息序列（AIMessage.tool_calls 与 ToolMessage 成对）。

    孤儿 tool_result（codex 的事件流没有配对的 tool_call 声明）补一个空
    AIMessage 钉住 tool_call_id，维持消息对的完整性——messages_ui 的渲染
    和 LangChain 的不变式都依赖成对。
    """
    msgs: list = [HumanMessage(content=prompt)]
    pending: set[str] = set()
    synth = 0
    for entry in ir or []:
        try:
            role = entry.get("role")
            if role == "assistant":
                calls = []
                for c in entry.get("tool_calls") or []:
                    cid = c.get("id")
                    if not cid:
                        synth += 1
                        cid = f"call_{synth}"
                    calls.append(
                        {
                            "id": cid,
                            "name": c.get("name") or "tool",
                            "args": c.get("args") or {},
                            "type": "tool_call",
                        }
                    )
                    pending.add(cid)
                text = entry.get("text") or ""
                if text or calls:
                    msgs.append(AIMessage(content=text, tool_calls=calls))
            elif role == "tool_result":
                tid = entry.get("id") or ""
                if tid not in pending:
                    synth += 1
                    tid = f"call_{synth}"
                    msgs.append(
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "id": tid,
                                    "name": entry.get("name") or "tool",
                                    "args": {},
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                    pending.add(tid)
                msgs.append(
                    ToolMessage(
                        content=str(entry.get("content") or ""),
                        name=entry.get("name") or "tool",
                        tool_call_id=tid,
                    )
                )
        except Exception:  # noqa: BLE001 — 单条坏条目不拖垮整段转录
            continue
    return msgs


def reconcile_running_delegations() -> int:
    """启动对账：runtime 重启会把后台委托协程一并带走——凡 type=delegation
    且 stop_reason=running 的存量条目一律改判 error（2026-10-05 真机事故）。
    返回修正条数。绝不抛异常。"""
    n = 0
    try:
        from .session_meta import _session_meta_list, _session_meta_patch

        for idx in paths.home().glob("projects/*/sessions/_index.json"):
            slug = idx.parent.parent.name
            for m in _session_meta_list(slug):
                if m.get("type") == "delegation" and m.get("stop_reason") == "running":
                    _session_meta_patch(slug, m["id"], {"stop_reason": "error"})
                    n += 1
        if n:
            try:
                from .server_shared import _log

                _log.info("delegation_reconciled_orphans=%d", n)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 — 对账绝不影响启动
        pass
    return n


def _write_checkpoint(slug: str, sid: str, msgs: list) -> None:
    ckpt = {
        "v": 1,
        "id": uuid.uuid4().hex,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "channel_values": {"messages": msgs},
        "channel_versions": {},
        "versions_seen": {},
    }
    FileCheckpointer(slug).put(
        {"configurable": {"thread_id": sid}},
        ckpt,
        {"source": "loop", "step": 1, "writes": None},
        {},
    )


def _result_note(result, timeout_s: int) -> str:
    stop = getattr(result, "stop_reason", "")
    if stop == "timeout":
        return f"[timeout] killed after {timeout_s}s"
    if stop == "error":
        return f"[error] {getattr(result, 'diagnostic', '') or 'unknown failure'}"
    return ""


def create_delegation(
    parent_session_id: str | None,
    project_slug: str | None,
    backend: str,
    mode: str,
    prompt: str,
    workspace: str,
    sid: str | None = None,
) -> str:
    """委托开始即建骨架（stop_reason="running" + 仅 prompt 的转录）。

    侧栏/回放立刻有锚点；finalize_delegation 在结束时回填完整转录与终态
    （checkpoint 追加新条目，history 读最后一条）。绝不抛异常，失败返回 ""。
    """
    try:
        slug = project_slug or "default"
        sid = sid or uuid.uuid4().hex
        now = time.time()
        found = _find_meta(parent_session_id or "") if parent_session_id else None
        parent_meta = found[0] if found else {}
        entry = {
            "id": sid,
            "title": f"{backend} · {(prompt or '').strip()[:40]}",
            "title_auto": False,
            "icon": _DELEGATION_ICONS.get(backend, "terminal"),
            "agent_id": parent_meta.get("agent_id"),
            "provider": backend,
            "model": backend,
            "workspace": workspace,
            "type": "delegation",
            "parent_session_id": parent_session_id,
            "depth": max(subagent_depth_of(parent_meta), 0) + 1,
            "backend": backend,
            "mode": mode,
            "stop_reason": "running",
            "created": now,
            "updated": now,
        }
        _session_meta_upsert(slug, entry)
        _prune_delegation_sessions(slug)
        # 初始转录只有 prompt——运行中打开回放不是空屏。
        _write_checkpoint(slug, sid, [HumanMessage(content=prompt or "")])
        if parent_session_id:
            try:
                from . import server_shared as shared

                shared.subagent_link_child(parent_session_id, sid)
                # 骨架行同步广播 subagent.spawned——侧栏立刻建行(docstring 承诺
                # 的"立刻可见"此前缺的就是这一步):前端 notifySubagentSpawned 凭
                # 事件合成占位行,否则要等下一次 reloadSessions(窗口聚焦等偶发
                # 时机)才浮出来。只发父会话 socket;引擎对 type=delegation 的
                # spawned 不加发起卡(实时进度已有 delegate_update 卡)。
                shared.spawn_bg(
                    shared._push_session_event(
                        parent_session_id,
                        "subagent.spawned",
                        {
                            "session_id": sid,
                            "parent_session_id": parent_session_id,
                            "goal": (prompt or "")[:120],
                            "constraints": "",
                            "acceptance": "",
                            "depth": entry["depth"],
                            "origin": "agent",
                            "title": entry["title"],
                            "mode": mode,
                            "agent_type": "",
                            "type": "delegation",
                            "backend": backend,
                            "icon": entry["icon"],
                        },
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        return sid
    except Exception:
        return ""


def _prune_delegation_sessions(slug: str) -> None:
    """委托归档超过 _MAX_DELEGATION_SESSIONS 时,把最旧的连 meta 带文件清掉
    (running 态的永不清理)。失败静默——修剪绝不影响委托主流程。"""
    try:
        from .session_meta import _session_meta_list, _session_meta_remove

        dels = [
            m
            for m in _session_meta_list(slug)
            if m.get("type") == "delegation" and m.get("stop_reason") != "running"
        ]
        dels.sort(
            key=lambda m: m.get("updated") or m.get("created") or 0, reverse=True
        )
        for m in dels[_MAX_DELEGATION_SESSIONS:]:
            sid = m.get("id") or ""
            if not sid:
                continue
            _session_meta_remove(slug, sid)
            try:
                os.unlink(paths.project_sessions_dir(slug) / f"{sid}.json")
            except OSError:
                pass
            try:
                shutil.rmtree(paths.session_files_dir(slug, sid), ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def finalize_delegation(
    sid: str,
    project_slug: str | None,
    result,
    prompt: str,
    timeout_s: int,
    terminal: bool = True,
) -> None:
    """回填：完整转录 checkpoint（+ terminal 时附终态注记/meta/原始归档）。

    ``terminal=False``：运行中的节流回填——只追加转录 checkpoint，meta 保持
    running，不写 [error]/[timeout] 注记。绝不抛异常。
    """
    if not sid:
        return
    try:
        slug = project_slug or "default"
        msgs = _ir_to_messages(getattr(result, "transcript", None) or [], prompt or "")
        if terminal:
            note = _result_note(result, timeout_s)
            if note:
                msgs.append(AIMessage(content=note))
        _write_checkpoint(slug, sid, msgs)
        if not terminal:
            return
        from .session_meta import _session_meta_patch

        _session_meta_patch(
            slug,
            sid,
            {
                "stop_reason": getattr(result, "stop_reason", "") or "",
                "model": (getattr(result, "meta", None) or {}).get("model") or "",
            },
        )
        raw = (getattr(result, "meta", None) or {}).get("_stdout_tail") or ""
        if raw:
            fdir = paths.session_files_dir(slug, sid)
            fdir.mkdir(parents=True, exist_ok=True)
            (fdir / "delegation-events.jsonl").write_text(
                raw[-_RAW_CAP:], encoding="utf-8"
            )
    except Exception:
        pass


def record_delegation(
    parent_session_id: str | None,
    project_slug: str | None,
    backend: str,
    mode: str,
    prompt: str,
    workspace: str,
    result,
    timeout_s: int,
) -> str:
    """一步式归档（create + finalize 组合）：兼容既有调用与测试。"""
    sid = create_delegation(
        parent_session_id, project_slug, backend, mode, prompt, workspace
    )
    if sid:
        finalize_delegation(sid, project_slug, result, prompt, timeout_s)
    return sid
