"""sessions 元数据域：tags / 已读未读 / PATCH / 运行状态 / 工具策略 /
上下文账本 / 建议 / 历史。

patch_session 覆盖标题、置顶、agent/workflow 换绑与 provider/model/思考
档位热切（原地换模，运行中 turn 下一 superstep 生效）。
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from ... import providers as prov_mod
from ... import server_shared as shared
from ...checkpointer import FileCheckpointer
from ..messages_ui import _messages_to_ui
from ...models import build_model, resolve_model_effort
from ...server_shared import _SESSIONS, _log
from ...session_meta import (
    _find_meta,
    _session_meta_list,
    _session_meta_patch,
    _session_meta_set_tags,
    _session_slug,
    _tags_fields,
)
from ...session_unread import mark_session_seen as _mark_session_seen
from ...session_unread import mark_session_unread as _session_unread_mark
from ._base import router


# ── Session label/icon（session-label-icon-design.md §1/§2）──────────────────
# icon 受控枚举 8 个；labels ≤3 个、strip 后非空且 ≤20 字符、去重。
SESSION_TAG_ICONS = frozenset(
    {"wrench", "bug", "doc", "release", "research", "chat", "warn", "star"}
)


@router.put("/api/sessions/{session_id}/tags")
async def set_session_tags(session_id: str, req: dict) -> dict:
    """全量覆盖会话的 icon/labels（null/[] 即清除）。纯视觉操作：touch=False
    不改变侧栏排序。非法值 422（带 message）。"""
    icon = req.get("icon")
    if icon is not None and icon not in SESSION_TAG_ICONS:
        raise HTTPException(
            status_code=422,
            detail={
                "ok": False,
                "error": f"invalid icon '{icon}'; must be one of {sorted(SESSION_TAG_ICONS)} or null",
            },
        )
    raw_labels = req.get("labels")
    if raw_labels is None:
        raw_labels = []
    if not isinstance(raw_labels, list) or not all(isinstance(x, str) for x in raw_labels):
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "error": "labels must be an array of strings"},
        )
    labels: list[str] = []
    for raw in raw_labels:
        label = raw.strip()
        if not label:
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "error": "labels must be non-empty after trimming whitespace"},
            )
        if len(label) > 20:
            raise HTTPException(
                status_code=422,
                detail={"ok": False, "error": f"label too long (max 20 chars): '{label}'"},
            )
        if label not in labels:  # 去重（strip 后比较）
            labels.append(label)
    if len(labels) > 3:
        raise HTTPException(
            status_code=422,
            detail={"ok": False, "error": f"too many labels (max 3): got {len(raw_labels)}"},
        )
    slug = _session_slug(session_id)
    if slug is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    updated = _session_meta_set_tags(slug, session_id, icon, labels)
    if updated is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    # 内存条目同步（免得 list 内存兜底分支透出旧值）
    s = _SESSIONS.get(session_id)
    if s is not None:
        if icon is None:
            s.pop("tag_icon", None)
        else:
            s["tag_icon"] = icon
        s["labels"] = list(labels)
    return {"ok": True, "session": {**updated, **_tags_fields(updated)}}


@router.post("/api/sessions/{session_id}/seen")
async def mark_session_seen_endpoint(session_id: str) -> dict:
    """会话已读上报（前端「正在看」成立时调用，幂等）。写入 touch=False：
    读取不改变侧栏排序。"""
    slug = _session_slug(session_id)
    if slug is None:
        return {"ok": False, "error": "not_found"}
    _mark_session_seen(slug, session_id)
    return {"ok": True}


@router.post("/api/sessions/{session_id}/unread")
async def mark_session_unread_endpoint(session_id: str) -> dict:
    """手动「标为未读」（2026-11-16，session 行右键/扇形菜单）：置
    forced_unread（session_unread），打开会话即随 seen 清除——与 IM 同
    语义。touch=False 不重排侧栏；未找到会话返回 not_found。"""
    slug = _session_slug(session_id)
    if slug is None:
        return {"ok": False, "error": "not_found"}
    ok = _session_unread_mark(slug, session_id)
    return {"ok": ok, **({} if ok else {"error": "not_found"})}


class PatchSessionRequest(BaseModel):
    title: str | None = None
    icon: str | None = None
    agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    workflow_id: str | None = None
    # 侧栏置顶（sidebar pin）。False 是合法值——_session_meta_patch 只过滤
    # None，所以取消置顶也能落盘。
    pinned: bool | None = None
    # 「接活」标注（worker 协议，扇形菜单入口）：main 会话是否接受派活。
    # 缺省 = 接活（main 本身也是 worker）；False = 用户显式关闭。只对 main
    # 生效，task-typed 天生 worker。
    worker: bool | None = None
    # 思考档位换档(设计 §2):非 None 即换,走与 provider/model 同一条热切
    # 路径(重建 model 对象原地换入,运行中 turn 下一 superstep 生效)。
    thinking_effort: str | None = None


@router.patch("/api/sessions/{session_id}")
async def patch_session(session_id: str, req: PatchSessionRequest) -> dict:
    s = _SESSIONS.get(session_id)
    stored = _find_meta(session_id) if s is None else None
    slug = s["project_slug"] if s else stored[1] if stored else "default"
    patch = req.model_dump()

    # Per-session model switch (composer model chip): validate before touching
    # meta. NON-structural switches are IN-PLACE (model-assignment-design.md
    # §3.2): the live graph resolves its model through
    # server_shared.make_session_model_getter at every LLM call, so swapping
    # the entry's model takes effect at the next superstep — a running turn
    # keeps streaming its current call on the OLD model, no reconnect needed.
    # Structural patches (agent/workflow rebinding) still pop the entry so the
    # next WS connect rebuilds the graph, same heal path as PUT /api/providers.
    # 思考档位换档(thinking-effort-design §2):非 None 即换("auto" 显式回
    # 缺省),归一成落盘值;与 provider/model 共用同一条热切路径。
    eff_raw = patch.get("thinking_effort")
    eff_changed = eff_raw is not None
    switched = (
        patch.get("provider") is not None
        or patch.get("model") is not None
        or eff_changed
    )
    new_model = None
    if switched:
        cur_meta = next(
            (m for m in _session_meta_list(slug) if m.get("id") == session_id), None
        ) or {}
        provider = (
            patch.get("provider") or (s or {}).get("model_provider") or cur_meta.get("provider")
        )
        if patch.get("provider") is not None and patch.get("model") is None:
            # provider change without an explicit model → that provider's
            # configured default, so the meta never keeps a foreign model.
            patch["model"] = prov_mod.model_for_provider(
                prov_mod.load_providers(), patch["provider"]
            )
        if patch.get("model") is None:
            # 只换档位/只换 provider 以外的组合:模型沿用会话现值(meta 名),
            # 不能回落 provider 默认把会话的钉选模型洗掉。
            patch["model"] = (s or {}).get("model_name") or cur_meta.get("model")
        # Validate explicit choices against the destination model. An inherited
        # choice can be unavailable after a model switch and rolls back to auto.
        saved_effort = (s or {}).get("thinking_effort") or cur_meta.get("thinking_effort")
        try:
            eff = resolve_model_effort(
                patch.get("model"), eff_raw if eff_changed else saved_effort, strict=eff_changed
            )
            patch["thinking_effort"] = eff or "auto"
            new_model = build_model(
                provider,
                patch.get("model"),
                session_id=session_id,
                thinking_effort=eff,
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    # Inspect the stored meta to honour the title_auto flag. Auto titles come
    # from the first user message (stream._touch_session_title); an explicit
    # rename sticks and an agent switch no longer renames.
    cur = next((m for m in _session_meta_list(slug) if m.get("id") == session_id), None)
    title_auto = (cur or {}).get("title_auto", True)
    if patch.get("title") is not None:
        title_auto = False
        # manual rename wins over a pending LLM subject title (title_gen)
        patch["title_llm_pending"] = False
    patch["title_auto"] = title_auto

    updated = _session_meta_patch(slug, session_id, patch)
    structural = patch.get("agent_id") is not None or patch.get("workflow_id") is not None
    if switched and structural:
        # agent/workflow 换绑是结构变更（persona/toolset/turn context 都变），
        # graph 必须重建：维持既有 pop → 下次连接 _ensure_session 重建。
        _SESSIONS.pop(session_id, None)
        s = None
    elif s is not None:
        if new_model is not None:
            # 模型热切原地更新（design §3.2）：内存条目换模型对象 + 绑定字段。
            # 运行中的 turn 不受打断——graph 的 getter 下一个 superstep 生效；
            # compaction 等读 session["model"] 的路径自动跟随。
            s["model"] = new_model
            s["model_provider"] = provider
            s["model_name"] = patch.get("model")
        for k, v in patch.items():
            # provider/model 走 meta 键名，条目里对应 model_provider/model_name，
            # 已在上面显式更新——跳过，避免写进错误键。
            if v is not None and k not in ("provider", "model"):
                s[k] = v
        s["title_auto"] = title_auto
    if new_model is not None:
        _log.info(
            "session_model_switched session=%s provider=%s model=%s effort=%s running=%s",
            session_id,
            provider,
            patch.get("model"),
            (s or {}).get("thinking_effort") if s else None,
            session_id in shared._RUNNING_TURNS,
        )
        await shared._push_session_event(
            session_id,
            "session.model_changed",
            {
                "session_id": session_id,
                "provider": provider,
                "model": patch.get("model"),
                # 思考档位随事件同步(设计 §6):其它已打开同会话的客户端据此
                # 更新 chip 角标;auto 也显式给,缺字段时前端保持旧值。
                "thinking_effort": patch["thinking_effort"],
                "effective": "next_step",
            },
        )
    return {
        "ok": True,
        "session": updated or (s and {k: v for k, v in s.items() if k != "graph"}),
    }


@router.get("/api/sessions/{session_id}/state")
async def get_session_state(session_id: str) -> dict:
    """运行时状态机快照（重连 reconcile 用；不持久化，重启回落 idle）。"""
    from ... import session_state

    snap = session_state.get_state(session_id)
    # 派生层：compacting/swapping 以显式写入为准，其余从注册表推导
    snap = {**snap, "state": session_state.derive_state(session_id)}
    return {"ok": True, **snap}


class ToolPolicyBody(BaseModel):
    mode: str = "all"          # all | minimal | custom
    mcp_disabled: list[str] = []
    # server -> graph-facing full names (mcp_<server>_<tool>). Tool-level
    # blacklist layered on top of the server-level one (2026-11-13 拍板).
    mcp_tools_disabled: dict[str, list[str]] = {}
    skills_disabled: list[str] = []


@router.get("/api/sessions/{session_id}/context_breakdown")
async def get_context_breakdown(session_id: str) -> dict:
    """上下文管理面板的账本：工具/技能/系统/历史的 token 组成 + 窗口阈值。

    只读、本地估算（tiktoken o200k_base），不调用模型；数字全标 ≈。
    口径与 TopBar 圆环/压缩阈值共用同一条解析链（usage.context_state /
    compaction._effective_compact_threshold）。
    """
    from ...context_breakdown import build as _build
    from ...session_meta import _resolve_session_meta

    snap = _resolve_session_meta(session_id)
    if not snap:
        return {"ok": False, "error": "session not found"}
    found = _find_meta(session_id)
    info = {**(found[0] if found else {}), **snap}
    if found:
        info["project_slug"] = found[1]
    try:
        return _build(session_id, info)
    except Exception as exc:  # noqa: BLE001 — 面板是辅助视图，不能拖垮会话
        _log.warning("context_breakdown failed session=%s: %s", session_id, exc)
        return {"ok": False, "error": str(exc)}


@router.put("/api/sessions/{session_id}/tool_policy")
async def set_tool_policy(session_id: str, body: ToolPolicyBody) -> dict:
    """Session Tool Policy（2026-11-12 P0）：黑名单制，下一 turn 生效。

    写 meta（真源）+ 运行时 dict；清空 mcp_tool_names 让下一轮的漂移
    检查触发 graph 重建（重建时按 policy 过滤）。运行中 turn 不热插拔。
    """
    from ...tool_policy import VALID_MODES

    if body.mode not in VALID_MODES:
        return {"ok": False, "error": "bad mode"}
    _m, _slug_dir = _find_meta(session_id) or (None, None)
    if _m is None:
        return {"ok": False, "error": "session not found"}
    # 工具级黑名单只接受 graph 面向的 mcp_* 全名——builtin 工具永远不可
    # 通过会话策略隐藏（2026-11-12 拍板 ②）。
    tools_disabled: dict[str, list[str]] = {}
    for server, names in (body.mcp_tools_disabled or {}).items():
        clean = sorted(
            {
                str(n)
                for n in (names or [])
                if isinstance(n, str) and n.startswith("mcp_")
            }
        )
        if clean:
            tools_disabled[str(server)] = clean
    patch = {
        "tool_policy": {
            "mode": body.mode,
            "mcp_disabled": sorted(set(body.mcp_disabled)),
            "mcp_tools_disabled": tools_disabled,
            "skills_disabled": sorted(set(body.skills_disabled)),
        }
    }
    _session_meta_patch(_slug_dir, session_id, patch)
    s = _SESSIONS.get(session_id)
    if s is not None:
        s["tool_policy"] = patch["tool_policy"]
        s["mcp_tool_names"] = []  # 触发漂移重建（refresh 应用过滤）
    return {"ok": True, **patch["tool_policy"]}


# ---- session history (rebuild the chat UI's block layout from checkpoints) ----


@router.get("/api/sessions/{session_id}/suggestions")
async def session_suggestions(session_id: str) -> dict:
    """V2 P2-1: actionable, execution-derived tips (never for automation)."""
    from ...suggestions import suggestions_for

    return suggestions_for(session_id)


@router.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str) -> dict:
    """Return the persisted chat history for a session, in the UI block format."""
    slug = _session_slug(session_id)
    if not slug:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    cfg = {"configurable": {"thread_id": session_id}}
    tup = await FileCheckpointer(slug).aget_tuple(cfg)
    if not tup or not tup.checkpoint:
        return {"ok": True, "messages": []}
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    attached = (tup.checkpoint.get("channel_values") or {}).get("attached_files") or []
    meta, _ = (_find_meta(session_id) or ({}, None))
    agent_id = meta.get("agent_id") if isinstance(meta, dict) else None
    last_error = (meta.get("last_error") or None) if isinstance(meta, dict) else None
    return {
        "ok": True,
        "messages": _messages_to_ui(
            messages, agent_id, attached, project_slug=slug, session_id=session_id
        ),
        "last_error": last_error or None,
    }
