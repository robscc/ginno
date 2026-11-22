"""sessions 包共享基座：全包唯一 router + 跨资源模块的会话基建。

绞杀者拆分（重构批次 2-1）的协议叶子，参照 commands/_base.py 范式：
``router`` 是全包唯一的 APIRouter 实例——各资源模块 import 同一对象注册
端点，保证 ``ginno_runtime.api.sessions.router`` 的聚合语义与拆分前的单
文件完全一致（tests/api/test_openapi_contract.py 以 router.routes 判定
契约归属，拆包后快照必须零漂移）。

其余成员是跨模块共享的会话基建：

- ``_stored_effort`` / ``_build_model_with_fallback``：思考档位落盘归一
  与带回退的模型构造（create / _ensure_session / patch 三条路径共用）；
- ``_default_title`` / ``_agent_icon``：新会话默认标题与图标；
- ``is_channel_session``：渠道会话只读闸（api/stream/ws.py 消费）；
- ``_first_agent_id`` / ``_ensure_session``：会话 bootstrap——重启后按
  磁盘 meta 懒重建内存条目（历史由 file checkpointer 恢复）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from ... import context_folders as cf
from ... import paths
from ... import providers as prov_mod
from ... import server_shared as shared
from ... import agents as agents_reg
from ...classifier.middlewares import build_middlewares
from ...graph import build_all_tools, build_graph
from ...models import build_model, normalize_effort, resolve_model_effort
from ...server_shared import _SESSIONS
from ...session_meta import _find_meta
from ..config import _agent_lookup

router = APIRouter()


def _stored_effort(value: Any) -> str:
    """落盘归一(thinking-effort-design §2):恒返回 "auto"|档位,不落 None
    ——``_session_meta_patch`` 过滤 None,回 auto 必须显式存 "auto" 字符串。"""
    return normalize_effort(value) or "auto"


def _build_model_with_fallback(
    provider: str,
    model_name: str | None,
    session_id: str | None = None,
    thinking_effort: str | None = None,
):
    """Build the session model, falling back to the global default config.

    Decision Q3 runtime side: an agent may still carry a binding that no
    longer resolves (config deleted/disabled, or its model left models[]).
    That must degrade, not crash the session: log a warning and rebuild from
    the default config + its default model. Re-raises only when the default
    itself is broken (nothing to fall back to).
    """
    try:
        return build_model(
            provider, model_name, session_id=session_id, thinking_effort=thinking_effort
        )
    except ValueError as e:
        dflt = prov_mod.get_default_provider()
        dflt_model = prov_mod.model_for_provider(prov_mod.load_providers(), dflt)
        if dflt == provider and (model_name or None) == (dflt_model or None):
            raise
        logging.getLogger(__name__).warning(
            "model build failed for provider=%s model=%s (%s); "
            "falling back to default config %s/%s",
            provider,
            model_name,
            e,
            dflt,
            dflt_model,
        )
        return build_model(
            dflt, dflt_model, session_id=session_id, thinking_effort=thinking_effort
        )


def _default_title(agent_id: str | None) -> str:
    a = _agent_lookup(agent_id)
    return f"{a.name} session" if a else "New Session"


def _agent_icon(agent_id: str | None) -> str:
    """新会话的图标 = agent 的图标；agent 未配置 icon（空串）时留空，由前端
    渲染为品牌鸭兜底（icons.tsx AgentIcon）。agent 不存在同样留空。"""
    a = _agent_lookup(agent_id)
    return a.icon if a else ""


# ---- Goal continuation driver (goal-design.md §4.3.3) ---------------------
# One asyncio task per session with an active goal. After every turn ends and
# the session goes idle, it injects a continuation message and starts the next
# turn HEADLESSLY (no client socket required — the user may have closed the
# window). Guards: user turns always win (turn lock + idle waits), pending
# permission interrupts stall continuation, a goal does not follow an agent
# switch (auto-pause). There is deliberately NO turn-count cap: context size
# is managed by the existing auto-compaction (E3).


def is_channel_session(session: dict | None) -> bool:
    """Channel sessions (wecom/dingtalk) converse on the IM side; the desktop
    renders them read-only (ChatStream ``isChannel``). The API must enforce the
    same gate — a UI-only readonly is bypassable by any WS client (2026-11-16).
    Legacy channel sessions predate the ``type`` marker; the ``channel`` field
    covers them until the adapter backfill runs. The channel adapters' own
    inbound path calls ``_run_stream`` in-process and is NOT gated here.
    """
    if not session:
        return False
    return session.get("type") == "channel" or bool(session.get("channel"))


# ---- session bootstrap ----


def _first_agent_id() -> str | None:
    lst = agents_reg.list_agents()
    return lst[0].id if lst else None


def _ensure_session(session_id: str) -> dict[str, Any] | None:
    """Return the in-memory session, lazily rebuilding its graph from the
    on-disk index meta when the runtime was restarted (so sessions survive
    restarts; history is restored by the file checkpointer via thread_id)."""
    s = _SESSIONS.get(session_id)
    if s:
        shared.session_registry_touch(session_id)
        return s
    found = _find_meta(session_id)
    if not found:
        return None
    meta, slug = found
    provider = meta.get("provider") or prov_mod.get_default_provider()
    model_name = meta.get("model") or prov_mod.model_for_provider(
        prov_mod.load_providers(), provider
    )
    # 思考档位随会话记录恢复(设计 §2):meta 落盘值 "auto"|档位,auto → None。
    stored_eff = resolve_model_effort(model_name, meta.get("thinking_effort")) or "auto"
    try:
        model = _build_model_with_fallback(
            provider,
            model_name,
            session_id,
            thinking_effort=None if stored_eff == "auto" else stored_eff,
        )
    except ValueError:
        return None
    # Derive the workspace from paths (not meta) so legacy sessions whose meta
    # still holds the shared "/tmp/gw" converge on the per-session dir without
    # rewriting the index.
    workspace = str(paths.session_files_dir(slug, session_id))
    # Restore the persisted mount set (context-folders-design.md): sessions
    # resume with exactly the folders they had before the restart.
    folder_ids = meta.get("context_folders") or []
    primary_id = meta.get("primary_folder") or None
    context_dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    from ...tool_policy import filter_mcp_tools as _ftp
    mcp_tools = _ftp(shared._mcp.all_langchain_tools() if shared._mcp else [], meta)
    # Restore the subagent depth so the structural spawn cap survives a runtime
    # restart (subagent-design.md §4.2) — the meta is the source of truth.
    sub_depth = meta.get("depth") if meta.get("type") == "subagent" else None
    # Re-derive the type-registry tightening (P3 contract 1) from the meta's
    # recorded agent_type: a type edited since spawn re-tightens; a type
    # DELETED since spawn loosens back to the parent persona (documented).
    restrict_tools: list[str] = []
    if sub_depth is not None:
        from ...subagent_types import restrict_for_meta

        restrict_tools = restrict_for_meta(meta)
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=slug,
        session_id=session_id,
        context_dirs=context_dirs,
        primary_path=primary_path,
        subagent_depth=sub_depth,
        restrict_tools=restrict_tools,
        # skill-paths design: restart rebuild restores session skill dirs.
        skill_dirs=meta.get("skill_dirs") or [],
    )
    router, tool_selector = build_middlewares(session_id)
    graph = build_graph(
        model=model,
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
        all_tools=all_tools,
        # 模型热切（design §3.2）：重启重建的会话同样支持运行中换模型。
        model_getter=shared.make_session_model_getter(session_id, model),
        router=router,
        tool_selector=tool_selector,
    )
    s = {
        "session_id": session_id,
        "project_slug": slug,
        "workspace": workspace,
        "agent_id": meta.get("agent_id"),
        "title": meta.get("title"),
        "icon": meta.get("icon"),
        "model_provider": provider,
        "model_name": model_name,
        "thinking_effort": stored_eff,
        "graph": graph,
        "model": model,
        "all_tool_names": [t.name for t in all_tools],
        "mcp_tool_names": [t.name for t in mcp_tools],
        "context_dirs": context_dirs,
        "primary_folder": primary_id,
        "primary_path": primary_path or "",
        "workflow_id": meta.get("workflow_id"),
        # 远端会话随重启恢复（meta 是真源）——漏了这行重启后就变回本地执行
        "remote_peer": meta.get("remote_peer"),
        "restrict_tools": restrict_tools,
        # Survive graph rebuilds: the WS readonly gate reads these from the
        # in-memory session, so a restarted runtime must restore them too.
        "type": meta.get("type"),
        "channel": meta.get("channel"),
        # Tool policy 随会话恢复（meta 真源 → 运行时 dict，供 turn 刷新/rebind 用）
        "tool_policy": meta.get("tool_policy"),
        # skill-paths design: restart rebuild must re-serve skill_dirs so the
        # turn layer keeps injecting AgentState.skill_dirs / skills index.
        "skill_dirs": meta.get("skill_dirs") or [],
    }
    if sub_depth is not None:
        s["type"] = "subagent"
        s["parent_session_id"] = meta.get("parent_session_id")
        s["depth"] = sub_depth
        shared.subagent_link_child(meta.get("parent_session_id") or "", session_id)
    shared.session_registry_put(session_id, s)
    return s
