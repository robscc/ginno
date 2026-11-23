"""sessions CRUD：建会话 / 列表 / 分页 / 搜索 / 单获取。

create_session 是会话的事实出生点（meta 落盘 + 内存注册 + graph 装配）；
list/page/search 共用 meta 索引 + unread/tags 装饰。路径注册顺序在本模块
内保持原文件相对顺序——/api/sessions/page、/api/sessions/search 必须先于
/api/sessions/{session_id} 注册，否则会被参数路由遮蔽。
"""

from __future__ import annotations

import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel

from ... import context_folders as cf
from ... import paths
from ... import providers as prov_mod
from ... import server_shared as shared
from ...agents.memory import ensure_agent_memory
from ...classifier.middlewares import build_middlewares
from ...graph import build_all_tools, build_graph
from ...lang import t
from ...model_tags import resolve_model_effort
from ...server_shared import _SESSIONS, _log
from ...session_meta import (
    _find_meta,
    _resolve_session_meta,
    _session_meta_list,
    _session_meta_upsert,
    _tags_fields,
    subagent_depth_of,
)
from ...session_unread import is_unread as _session_is_unread
from ...subagent_scheduler import SUBAGENT_MAX_DEPTH
from ..config import _agent_lookup
from ._base import (
    _build_model_with_fallback,
    _agent_icon,
    _default_title,
    router,
)


class CreateSessionRequest(BaseModel):
    project_slug: str
    workspace: str
    agent_id: str | None = None
    title: str | None = None
    icon: str | None = None
    # Session kind (docs/floating-window-design.md §1.1): "quick" marks sessions
    # created by the floating quick-chat window; None/absent = regular session.
    type: str | None = None
    # 渠道来源标记（wecom-connector-design.md / dingtalk-connector-design.md）：
    # 由 channels.ensure_channel_session 写入，值如 "wecom"/"dingtalk"。与
    # type="channel" 成对出现——前端据此渲染只读视图（对话在 IM 侧进行）。
    channel: str | None = None
    provider: str | None = None
    model: str | None = None
    # 远端会话（remote-session-design.md R1/B）：挂 A2A peer slug 后，本会话
    # 每轮都在该远端节点执行，本地不跑图。空/缺省=本地。与 agent_id 同层落盘。
    remote_peer: str | None = None
    # Context folder mounts (context-folders-design.md): library ids; the
    # session starts with these attached. Unknown ids are dropped silently.
    context_folders: list[str] = []
    primary_folder: str | None = None
    # Session-scoped skill dirs (skill-paths design): SKILL.md roots beyond
    # the three fixed discovery tiers. Validated at creation (absolute dir);
    # Invalid entries fail with HTTP 422; no implicit filesystem grants.
    skill_paths: list[str] = []
    # Bound workflow for workflow-dev refine sessions (docs/workflow-dsl-design.md
    # §8.3). Injected into every turn's [turn context] as the current DSL.
    workflow_id: str | None = None
    # Subagent parentage (subagent-design.md §4). Set by the runtime scheduler
    # (subagent_scheduler.create_subagent), never by ordinary clients: the child
    # inherits the parent's slug-context and its depth caps the toolset.
    parent_session_id: str | None = None
    subagent: dict | None = None
    # Subagent type-registry tightening (P3 contract 1): fnmatch patterns the
    # matched type imposes ON TOP of the parent persona's tools_allow (the
    # persona filter keeps running at request time; this one filters the bound
    # toolset at graph-build time). Set only by the scheduler's spawn path.
    restrict_tools: list[str] = []
    # legacy aliases
    model_provider: str | None = None
    model_name: str | None = None
    # 思考档位(thinking-effort-design §2):"off"/"low"/"medium"/"high";
    # None/"auto" = 跟随 provider 配置(现状行为)。解析链:req > agent 默认
    # > auto(见 _resolve_thinking_effort)。
    thinking_effort: str | None = None


def _validate_skill_paths(paths: list[str], context: dict | None = None) -> list[str]:
    from ...skills.loader import session_access_roots, validate_skill_paths

    try:
        return validate_skill_paths(paths, session_access_roots(context or {}))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _resolve_provider_model(req: CreateSessionRequest) -> tuple[str, str, str | None]:
    agent = _agent_lookup(req.agent_id)
    providers = prov_mod.load_providers()

    def _enabled(pid: str | None) -> bool:
        return bool(pid) and bool((providers.get(pid) or {}).get("enabled"))

    # Prefer an *enabled* provider, in this order:
    #   1. explicit request (newSession opts / pin app)
    #   2. the agent's DELIBERATE provider choice — i.e. one that DIFFERS from
    #      the global default (设置 → 通用 → 默认模型提供商). Seed agents carry
    #      provider="custom", which equals the seeded default; letting that
    #      placeholder outrank the user's global choice made the General
    #      Settings selector dead (2026-10-02 fix). An agent binding that
    #      matches the default is the same outcome either way.
    #   3. the enabled global default ("enable a provider and use it" without
    #      editing every agent still works).
    default_pid = prov_mod.get_default_provider()
    from ...agents.registry import provider_is_deliberate

    candidates = [
        req.provider,
        req.model_provider,
        (agent.provider if provider_is_deliberate(agent) else None),
        default_pid,
    ]
    provider = next((c for c in candidates if _enabled(c)), None) or prov_mod.get_default_provider()
    model = (
        req.model
        or req.model_name
        or (agent.model if agent and agent.model else None)
        or prov_mod.model_for_provider(providers, provider)
    )
    return provider, model, (agent.id if agent else req.agent_id)


def _resolve_thinking_effort(
    req: CreateSessionRequest, model: str | None = None, *, cfg: dict | None = None,
) -> str:
    """创建时档位解析链(§9 P1):显式请求 > agent 默认 > auto。"""
    if req.thinking_effort is not None:
        return resolve_model_effort(cfg, model, req.thinking_effort, strict=True) or "auto"
    agent = _agent_lookup(req.agent_id)
    return resolve_model_effort(cfg, model, agent.thinking_effort if agent else None) or "auto"


# ---- sessions CRUD ----


@router.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict:
    provider, model_name, agent_id = _resolve_provider_model(req)
    # 思考档位(设计 §2):解析链显式 > agent 默认;落盘值恒 "auto"|档位,
    # build 时 "auto" 归 None(跟随 provider 配置)。
    try:
        effort_stored = _resolve_thinking_effort(
            req, model_name, cfg=prov_mod.load_providers().get(provider),
        )
    except ValueError as e:
        return {"error": str(e), "ok": False}
    effort = None if effort_stored == "auto" else effort_stored
    session_id = uuid.uuid4().hex
    try:
        model = _build_model_with_fallback(
            provider, model_name, session_id, thinking_effort=effort
        )
    except ValueError as e:
        return {"error": str(e), "ok": False}

    # Session Tool Policy（2026-11-12 P0）：显式传入优先；subagent/worker
    # 继承父会话（子不宽于父）；普通会话缺省 all（现状零变化）。
    from ...tool_policy import filter_mcp_tools, inherit, parse

    _tp_src: dict = {"tool_policy": req.tool_policy} if getattr(req, "tool_policy", None) else {}
    if req.parent_session_id and not _tp_src:
        _parent_meta, _ = _find_meta(req.parent_session_id) or ({}, None)
        _tp_src = inherit(_parent_meta or {})
    mcp_tools = filter_mcp_tools(
        shared._mcp.all_langchain_tools() if shared._mcp else [], _tp_src
    )
    # Subagent depth (subagent-design.md §4.2): one above the parent's; the
    # depth caps the toolset STRUCTURALLY (a depth-2 child never gets
    # spawn_subagent) and is rejected outright beyond it.
    #
    # AgentTeam 例外（agent-team-design.md §3.2）：``type="team-member"`` 的会话
    # 也带 parent_session_id（指向主会话，复用级联删除 + 侧栏树），但它**不是**
    # subagent——成员是常驻实体，不吃 subagent 的 depth 上限、不被塞 subagent
    # 载荷、工具集不因 depth 收紧。所以这里显式跳过 subagent 处理。
    is_team_member = req.type == "team-member"
    sub_depth: int | None = None
    if req.parent_session_id and not is_team_member:
        parent_meta, _ = _find_meta(req.parent_session_id) or ({}, None)
        sub_depth = subagent_depth_of(parent_meta) + 1
        if sub_depth > SUBAGENT_MAX_DEPTH:
            return {
                "ok": False,
                "error": (
                    t(
                        f"subagent nesting depth limit reached ({SUBAGENT_MAX_DEPTH + 1} levels)",
                        f"subagent 嵌套深度已达上限（{SUBAGENT_MAX_DEPTH + 1} 层）",
                    )
                ),
            }
    # Every session gets its own files directory, created now and PRESERVED on
    # delete. It supersedes the client-supplied `workspace` (a shared, non-
    # session-scoped path) as the authoritative home for this session's files.
    session_dir = paths.session_files_dir(req.project_slug, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    workspace = str(session_dir)
    # Context folder mounts: filter to known library ids; a primary must be
    # one of the mounts (context-folders-design.md §4.1).
    folder_ids = [fid for fid in (req.context_folders or []) if cf.get_folder(fid)]
    primary_id = req.primary_folder if req.primary_folder in folder_ids else None
    context_dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    # skill-paths design: validate BEFORE build_all_tools so use_skill binds
    # the same dirs the WS layer will re-serve from meta["skill_dirs"].
    skill_dirs = _validate_skill_paths(
        req.skill_paths, {"workspace": workspace, "context_dirs": context_dirs}
    )
    for d in context_dirs:
        if not d.get("missing"):
            cf.touch_folder(d["id"])
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=req.project_slug,
        session_id=session_id,
        context_dirs=context_dirs,
        primary_path=primary_path,
        subagent_depth=sub_depth,
        restrict_tools=req.restrict_tools,
        skill_dirs=skill_dirs,
    )
    router, tool_selector = build_middlewares(session_id)
    graph = build_graph(
        model=model,
        project_slug=req.project_slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
        all_tools=all_tools,
        # 模型热切（design §3.2）：graph 每次调用解析当前模型，PATCH 原地
        # 换模型对运行中的 turn 也生效。
        model_getter=shared.make_session_model_getter(session_id, model),
        router=router,
        tool_selector=tool_selector,
    )
    ag = _agent_lookup(agent_id)
    if ag:
        ensure_agent_memory(ag.id, ag.name)
    title = req.title or _default_title(agent_id)
    title_auto = not bool(req.title)  # auto title follows the active agent
    icon = req.icon or _agent_icon(agent_id)
    meta = {
        "id": session_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "agent_id": agent_id,
        "provider": provider,
        "model": model_name,
        "thinking_effort": effort_stored,
        "workspace": workspace,
        "context_folders": folder_ids,
        "primary_folder": primary_id,
        "workflow_id": req.workflow_id,
        # 远端会话（remote-session-design.md R1）：挂 peer slug → 每轮在远端跑
        "remote_peer": (req.remote_peer or "").strip() or None,
        "created": time.time(),
        "updated": time.time(),
    }
    # type 主动标注（2026-10-29 用户决策）：main 也是一等 worker 类型，创建
    # 即写 "main"——不再靠「缺 type = main」反推（新会话类型漏写 type 会被
    # 静默当成 main 放进 worker 派活白名单）。特殊类型（subagent/team-member/
    # channel…）仍由各自分支显式覆盖。
    meta["type"] = req.type or "main"
    if _tp_src:
        meta["tool_policy"] = _tp_src["tool_policy"]
    # skill-paths design: session-scoped skill dirs → meta["skill_dirs"]
    # (the WS turn layer reads this key to build AgentState.skill_dirs and
    # the WorldState skills index). Validation happened above build_all_tools
    # so the use_skill tool binds the same dirs.
    if skill_dirs:
        meta["skill_dirs"] = skill_dirs
    if req.channel:
        meta["channel"] = req.channel
    if is_team_member:
        # 团队成员：带 parent 链（指向主会话）以复用级联删除与侧栏树，但
        # **不**写 subagent 载荷、不吃 depth（§3.2）。parent 链是磁盘索引上的
        # 事实来源——_session_meta_descendants 只认 parent_session_id、不看类型。
        meta["parent_session_id"] = req.parent_session_id
    if sub_depth is not None:
        # Subagent meta (contract 1): the parent link + the subagent payload
        # are part of the session meta the list API and the frontend read.
        meta["type"] = "subagent"
        meta["parent_session_id"] = req.parent_session_id
        meta["depth"] = sub_depth
        meta["subagent"] = req.subagent or {
            "goal": "",
            "constraints": "",
            "acceptance": "",
            "origin": "agent",
            "status": "running",
            "result_summary": "",
        }
    _session_meta_upsert(req.project_slug, meta)
    _log.info(
        "session_create session=%s agent=%s provider=%s model=%s title=%r folders=%d",
        session_id,
        agent_id,
        provider,
        model_name,
        title,
        len(folder_ids),
    )
    s_entry = {
        "session_id": session_id,
        "project_slug": req.project_slug,
        "workspace": workspace,
        "agent_id": agent_id,
        "title": title,
        "title_auto": title_auto,
        "icon": icon,
        "model_provider": provider,
        "model_name": model_name,
        # 思考档位(设计 §2):条目里存落盘值("auto"|档位),PATCH 换档时
        # 重建 model 对象并同步此键。
        "thinking_effort": effort_stored,
        "graph": graph,
        # WorldState inputs (plan C1): the model for compaction summaries and
        # tool-name rosters for the agent/mcp sections' snapshots.
        "model": model,
        "all_tool_names": [t.name for t in all_tools],
        "mcp_tool_names": [t.name for t in mcp_tools],
        # Context folder mounts (context-folders-design.md)
        "context_dirs": context_dirs,
        # Session-scoped skill dirs (skill-paths design) — mirrors meta so the
        # WS turn layer can build AgentState.skill_dirs / skills index.
        "skill_dirs": skill_dirs,
        "primary_folder": primary_id,
        "primary_path": primary_path or "",
        "workflow_id": req.workflow_id,
        # 远端会话（remote-session-design.md R1）：_run_stream 顶部读此字段
        # 决定本轮是否在远端 peer 执行。内存条目与落盘 meta 必须一致。
        "remote_peer": (req.remote_peer or "").strip() or None,
        "type": req.type or "main",
        # Channel marker rides the in-memory entry too — the WS readonly gate
        # (is_channel_session) must see it without a disk round-trip, including
        # legacy sessions that carry only `channel` (no type marker).
        "channel": req.channel,
        # Type-registry tightening survives graph rebuilds via this entry
        # (turn.py's WorldState roster re-reads it too).
        "restrict_tools": [p for p in (req.restrict_tools or []) if isinstance(p, str) and p.strip()],
    }
    if is_team_member:
        # 内存条目也带 parent 链（团队树遍历/删除用），但**不**进 subagent 反向
        # 索引 _SESSION_CHILDREN——那是 subagent 调度器（stop 级联、完成门）专用，
        # 成员是常驻实体不参与 subagent 调度（§5.5 边界）。
        s_entry["parent_session_id"] = req.parent_session_id
    if sub_depth is not None:
        s_entry["parent_session_id"] = req.parent_session_id
        s_entry["depth"] = sub_depth
        # reverse index for the scheduler's tree walks (server_shared)
        shared.subagent_link_child(req.parent_session_id or "", session_id)
    shared.session_registry_put(session_id, s_entry)
    # Mods/classic SessionStart (claude-code-mods-design.md §5.3): classic
    # hooks first, the mods broker after; both observe-only, never fatal.
    try:
        from ...mods.events import dispatch_session_start

        await dispatch_session_start(session_id, meta)
    except Exception:
        _log.exception("mods_session_start_failed session=%s", session_id)
    # Vibe Island notch:SessionStart(卡片出现)。子会话在模块内被过滤;
    # 开关关闭时 no-op,绝不抛。
    try:
        from ...vibe_island import session_started

        session_started(session_id)
    except Exception:
        _log.exception("vibe_island_session_start_failed session=%s", session_id)
    # return the meta shape (with `id`) so the frontend SessionMeta matches
    return _with_remote_peer_name({**meta, "ok": True})


@router.get("/api/sessions")
async def list_sessions(project_slug: str | None = None) -> list[dict]:
    slug = project_slug or "default"
    on_disk = _session_meta_list(slug)
    if on_disk:
        # unread 装饰：服务端收件箱语义（session_unread），侧栏紫点的单一事实源
        return [
            _with_remote_peer_name(
                {**m, "unread": _session_is_unread(m), **_tags_fields(m)}
            )
            for m in on_disk
        ]
    return [
        {
            **{k: v for k, v in s.items() if k != "graph"},
            "unread": _session_is_unread(s),
            **_tags_fields(s),
        }
        for s in _SESSIONS.values()
        if s.get("project_slug") == slug
    ]


@router.get("/api/sessions/page")
async def page_sessions(
    limit: int = 50,
    offset: int = 0,
    project_slug: str | None = None,
    include_archived: bool = False,
) -> dict:
    """Paged session list (V2 P3-1).

    The full list stays available for the sidebar (it needs every row for
    grouping + archive counts); this endpoint is for callers that only want a
    window — and it reports the total, which the array endpoint cannot.
    """
    slug = project_slug or "default"
    rows = [
        {**m, "unread": _session_is_unread(m), **_tags_fields(m)}
        for m in _session_meta_list(slug)
    ]
    if not include_archived:
        rows = [r for r in rows if not r.get("archived")]
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    total = len(rows)
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    return {
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "sessions": rows[offset : offset + limit],
    }


@router.get("/api/sessions/search")
async def search_sessions(
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    project_slug: str | None = None,
) -> dict:
    """Server-side session search (P1-3).

    The client used to filter all 179+ rows in memory; with pagination the
    sidebar can stay responsive as history grows. Matches title (case
    insensitive) and exact/partial id. Archived sessions are searchable —
    the whole point of archiving is that they remain findable.
    """
    slug = project_slug or "default"
    rows = [
        {**m, "unread": _session_is_unread(m), **_tags_fields(m)}
        for m in _session_meta_list(slug)
    ]
    needle = (q or "").strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle in str(r.get("title") or "").lower()
            or needle in str(r.get("id") or "").lower()
        ]
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    total = len(rows)
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    return {
        "ok": True,
        "total": total,
        "limit": limit,
        "offset": offset,
        "sessions": rows[offset : offset + limit],
    }


def _remote_peer_name(slug: str | None) -> str | None:
    """远端会话执行方的**显示名**（PeerStore 就地解析；找不到回 slug）。

    为什么在后端解析：前端模型 chip/远端徽标此前各自 useA2aPeers 异步拉取
    再按 slug 找 name，拉取落定前的窗口只能回退 slug——i18n 模板
    ``model-remote-title``/``remote-badge-title`` 的 ``{name}`` 占位符因此
    闪现 slug（test_remote_session_model_chip_disabled flaky 的根因）。
    session 载荷在真源就地带上解析结果，前端优先用它，竞态窗口归零；
    「解析不到显示 slug」的回退语义收归后端单点。
    """
    if not slug:
        return None
    try:
        from ...a2a.peers import PeerStore

        rec = PeerStore().find(slug)
        return (rec.name or slug) if rec else slug
    except Exception:  # noqa: BLE001 — 读失败回 slug，不让会话载荷崩
        return slug


def _with_remote_peer_name(m: dict) -> dict:
    """session 载荷出口统一补 remote_peer_name（非远端会话零成本短路）。"""
    slug = m.get("remote_peer")
    if not slug:
        return m
    out = {**m, "remote_peer_name": _remote_peer_name(slug)}
    return out


@router.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    m = _resolve_session_meta(session_id)
    if m is None:
        # A literal JSON `null` with 200 made `res.json()` consumers blow up on
        # field access; an unknown session is a 404 (contract: API errors are
        # JSON with a real status code).
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    return _with_remote_peer_name(
        {k: v for k, v in m.items() if k != "graph"}
    ) | {"unread": _session_is_unread(m)} | _tags_fields(m)
