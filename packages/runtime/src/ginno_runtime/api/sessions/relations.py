"""sessions 关联域：fork、linked 投影/挂载/解除、context folders 全套。

apply_session_context 是 PUT /context 与 /mount 内建命令的共享核心——
落盘挂载集并重建活跃 graph（_apply_context_to_live_session）。
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel

from ... import context_folders as cf
from ... import server_shared as shared
from ...classifier.middlewares import build_middlewares
from ...graph import build_all_tools, build_graph
from ...server_shared import _SESSIONS, _log, _push_session_event
from ...session_meta import _find_meta, _session_meta_patch
from ._base import router


@router.post("/api/sessions/{session_id}/fork")
async def fork_session_endpoint(session_id: str) -> dict:
    """把会话复制为新会话（侧栏扇形菜单 Fork 按钮；/fork 命令的 REST 面）。

    与 IM 的 /fork 同核心（commands.registry.fork_session_core）：busy-gate
    带泄漏自愈、一会话一副本幂等、checkpoint 全量搬运。返回新会话 id，
    前端直接落进去。
    """
    from ...commands.registry import fork_session_core

    found = _find_meta(session_id)
    if not found:
        return {"ok": False, "error": "not_found"}
    meta, slug = found
    session = _SESSIONS.get(session_id) or {}
    r = await fork_session_core(session_id, slug, meta, session)
    if not r.get("ok"):
        # busy/create_failed/seed_failed —— error 码直传，前端摊提示
        return {"ok": False, "error": r.get("error") or "failed"}
    out = {"ok": True, "new_id": r["new_id"]}
    if r.get("copied") is not None:
        out["copied"] = r["copied"]
    if r.get("existing"):
        out["existing"] = True
    return out


@router.get("/api/sessions/{session_id}/linked")
async def get_linked_sessions(session_id: str) -> dict:
    """关联会话投影(topbar-unification.md §3):当前会话视野里的成员
    (spawned/delegated/assigned/watching/mounted)+ incoming(谁在派我)。
    面板打开 2s 轮询、徽标 5s 轮询——纯内存/索引读,无副作用。"""
    from ...session_links import linked_report

    rep = linked_report(session_id)
    if not rep.get("ok"):
        raise HTTPException(status_code=404, detail=rep)
    return rep


class MountLinkedRequest(BaseModel):
    target: str


@router.post("/api/sessions/{session_id}/linked")
async def mount_linked_session(session_id: str, req: MountLinkedRequest) -> dict:
    """手动挂载一个会话进当前对话视野(rel=mounted,P2)。"""
    from ...session_links import mount_linked

    out = mount_linked(session_id, req.target)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out)
    return out


@router.delete("/api/sessions/{session_id}/linked/{target}")
async def detach_linked_session(session_id: str, target: str) -> dict:
    """解除关联(detach):user 成员移出 linked_user,auto 成员落 linked_hidden。
    只断视野,不动会话本体。"""
    from ...session_links import detach_linked

    out = detach_linked(session_id, target)
    if not out.get("ok"):
        raise HTTPException(status_code=400, detail=out)
    return out


# ---- session context folders (context-folders-design.md §4.3) ---------------


class PutSessionContextRequest(BaseModel):
    folder_ids: list[str] = []
    primary_id: str | None = None


def _apply_context_to_live_session(
    s: dict, dirs: list[dict], primary_id: str | None, primary_path: str | None
) -> None:
    """Rebind a live session's tools/graph to a new mount set."""
    slug = s.get("project_slug") or "default"
    workspace = str(s.get("workspace") or "")
    s["context_dirs"] = dirs
    s["primary_folder"] = primary_id
    s["primary_path"] = primary_path or ""
    from ...tool_policy import filter_mcp_tools as _ftp
    mcp_tools = _ftp(shared._mcp.all_langchain_tools() if shared._mcp else [], s)
    all_tools = build_all_tools(
        mcp_tools,
        workspace=workspace,
        project_slug=slug,
        session_id=s.get("session_id", ""),
        context_dirs=dirs,
        primary_path=primary_path,
        # Keep the structural spawn cap across rebuilds (subagent-design.md §4.2).
        subagent_depth=s.get("depth") if s.get("type") == "subagent" else None,
        # And the type-registry tightening (P3 contract 1).
        restrict_tools=s.get("restrict_tools") or [],
        skill_dirs=s.get("skill_dirs") or [],
    )
    router, tool_selector = build_middlewares(s.get("session_id", ""))
    s["graph"] = build_graph(
        model=s["model"],
        project_slug=slug,
        workspace=workspace,
        mcp_tools=mcp_tools,
        hook_dispatcher=shared._hooks,
        all_tools=all_tools,
        # 模型热切：重建也不能把模型重新烤死（design §3.2），继续走 getter。
        model_getter=shared.make_session_model_getter(s.get("session_id", ""), s["model"]),
        router=router,
        tool_selector=tool_selector,
    )
    s["all_tool_names"] = [t.name for t in all_tools]
    s["mcp_tool_names"] = [t.name for t in mcp_tools]


def apply_session_context(
    session_id: str, folder_ids: list[str], primary_id: str | None
) -> dict:
    """Persist a session's mount set and rebuild its live graph (shared by
    ``PUT /api/sessions/{id}/context`` and the ``/mount`` builtin command)."""
    s = _SESSIONS.get(session_id)
    found = None if s else _find_meta(session_id)
    if s is None and found is None:
        return {"ok": False, "error": "unknown session"}
    slug = s["project_slug"] if s else found[1]

    folder_ids = [fid for fid in (folder_ids or []) if isinstance(fid, str) and fid]
    unknown = [fid for fid in folder_ids if cf.get_folder(fid) is None]
    if unknown:
        return {"ok": False, "error": f"Unknown folder id: {unknown}"}
    if primary_id and primary_id not in folder_ids:
        primary_id = None  # primary must be one of the mounts

    # "" clears a stale primary (_session_meta_patch drops None values).
    _session_meta_patch(
        slug,
        session_id,
        {"context_folders": folder_ids, "primary_folder": primary_id or ""},
    )
    dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    for d in dirs:
        if not d.get("missing"):
            cf.touch_folder(d["id"])

    if s is not None:
        _apply_context_to_live_session(s, dirs, primary_id, primary_path)

    # Let connected clients refresh their mount chip without a full refetch.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(
            _push_session_event(
                session_id,
                "session.context",
                {"context_folders": folder_ids, "primary_folder": primary_id},
            )
        )
    except RuntimeError:
        pass  # sync caller without a loop; clients reconcile on refetch

    _log.info(
        "session_context session=%s folders=%s primary=%s",
        session_id,
        folder_ids,
        primary_id,
    )
    return {
        "ok": True,
        "session": {
            "id": session_id,
            "context_folders": folder_ids,
            "primary_folder": primary_id,
        },
        "context_dirs": dirs,
        "primary_path": primary_path,
    }


@router.get("/api/sessions/{session_id}/context")
async def get_session_context(session_id: str) -> dict:
    """Read the session's mount set (read companion of the PUT above).

    Without this route the GET fell through to the SPA catch-all and answered
    200 HTML — clients reading it got a parse error instead of a 404.
    """
    s = _SESSIONS.get(session_id)
    found = None if s else _find_meta(session_id)
    if s is None and found is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    meta = s or found[0]
    folder_ids = list(meta.get("context_folders") or [])
    primary_id = meta.get("primary_folder") or None
    dirs, primary_path = cf.resolve_session_dirs(folder_ids, primary_id)
    return {
        "ok": True,
        "session": {
            "id": session_id,
            "context_folders": folder_ids,
            "primary_folder": primary_id,
        },
        "context_dirs": dirs,
        "primary_path": primary_path,
    }


@router.put("/api/sessions/{session_id}/context")
async def put_session_context(session_id: str, req: PutSessionContextRequest) -> dict:
    """Replace the session's mount set (idempotent full replacement)."""
    return apply_session_context(session_id, req.folder_ids, req.primary_id)
