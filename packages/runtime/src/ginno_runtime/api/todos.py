"""Todos endpoints: the global daily list plus provider sync (pull/push via
todo-pull/todo-push workflow runs)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import server_shared as shared
from .. import workflows as wf_store
from ..server_shared import _log, _push_global_event
from ..todos import providers as todo_providers
from ..todos import store as todo_store
from ..todos import sync_ledger
from .workflows import _run_workflow_bg, _spawn_run_task

router = APIRouter()


@router.get("/api/todos")
async def list_todos_endpoint() -> list[dict]:
    return todo_store.list_todos()


@router.post("/api/todos")
async def create_todo_endpoint(data: dict) -> dict:
    try:
        todo = todo_store.create_todo(data)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    await _push_global_event("todos.changed", {})
    return {"ok": True, "todo": todo}


@router.patch("/api/todos/{todo_id}")
async def update_todo_endpoint(todo_id: str, data: dict) -> dict:
    before = next((t for t in todo_store.list_todos() if t["id"] == todo_id), None)
    updated = todo_store.update_todo(todo_id, data)
    if updated is None:
        return {"ok": False, "error": "not found"}
    # Local done → platform: for every ext ref whose provider has auto_push,
    # trigger a todo-push workflow run (fully automatic; the checkbox IS the
    # confirmation). Failures never roll back the local state — the ledger
    # records them and the panel offers retry.
    if data.get("done") and before is not None and not before.get("done"):
        last_run = None
        for item in updated.get("ext") or []:
            pid = item.get("provider") or ""
            if not item.get("id"):
                continue
            prov = todo_providers.get_todo_provider(pid)
            if not prov or not prov.get("auto_push", True):
                continue
            ready, why = _provider_ready(prov)
            if not ready:
                _log.warning("todo_push_skipped todo=%s provider=%s reason=%s", todo_id, pid, why)
                continue
            run = _trigger_todo_workflow(
                "todo-push",
                prov,
                {
                    "ext_id": str(item["id"]),
                    "title": updated["title"],
                    "url": str(item.get("url") or ""),
                },
            )
            if run:
                sync_ledger.append(todo_id, pid, str(item["id"]), "push", run["id"])
                last_run = run
        if last_run:
            updated = (
                todo_store.update_todo(todo_id, {"links": {"workflow_id": last_run["id"]}})
                or updated
            )
    await _push_global_event("todos.changed", {})
    return {"ok": True, "todo": updated}


def _trigger_todo_workflow(wf_id: str, prov: dict, ctx: dict) -> dict | None:
    """Start a todo-pull/todo-push run with the provider's skill/mcp resolved
    into context (the generic agent node injects the skill and unlocks the
    provider's MCP server tools via {{mcp}})."""
    wf = wf_store.get_def(wf_id)
    if not wf:
        return None
    skill = todo_providers.resolve_skill_for(prov["id"], prov)
    override = {
        **ctx,
        "provider": prov["id"],
        "skill": skill or prov["id"],
        "mcp": str(prov.get("mcp") or ""),
    }
    # Persist the override so a retry re-runs with the same provider/skill/mcp.
    run = wf_store.create_run(wf, context_override=override)
    _spawn_run_task(run["id"], _run_workflow_bg(run["id"], wf_id, override, None))
    return run


def _provider_ready(prov: dict) -> tuple[bool, str]:
    """A provider can sync iff it has an injectable skill OR its MCP server is
    connected with tools. Gives actionable errors instead of pointless runs."""
    if todo_providers.resolve_skill_for(prov["id"], prov):
        return True, ""
    srv = prov.get("mcp")
    if srv:
        if shared._mcp and shared._mcp.server_tools(srv):
            return True, ""
        return False, f"MCP 服务未连接或无工具: {srv}"
    return False, f"provider {prov['id']} 既无 skill 也无可用 MCP（settings → todo_providers）"


@router.get("/api/todo-providers")
async def list_todo_providers_endpoint() -> dict:
    """Discovered external TODO platforms (skill declarations + settings)."""
    return {"ok": True, "providers": todo_providers.list_todo_providers()}


@router.get("/api/todos/sync-status")
async def todo_sync_status_endpoint() -> dict:
    """Recent todo<->platform sync events (panel badges / retry affordance)."""
    return {"ok": True, "entries": sync_ledger.latest(100)}


@router.post("/api/todos/pull")
async def todo_pull_endpoint(data: dict) -> dict:
    """Pull direction: mirror a provider's open todos into the local list."""
    pid = (data or {}).get("provider") or ""
    prov = todo_providers.get_todo_provider(pid)
    if not prov:
        return {"ok": False, "error": f"unknown todo provider: {pid}"}
    ready, why = _provider_ready(prov)
    if not ready:
        return {"ok": False, "error": why}
    run = _trigger_todo_workflow("todo-pull", prov, {})
    if not run:
        return {"ok": False, "error": "todo-pull workflow missing"}
    sync_ledger.append("", pid, "", "pull", run["id"])
    return {"ok": True, "run": run}


@router.post("/api/todos/{todo_id}/push")
async def todo_push_endpoint(todo_id: str, data: dict) -> dict:
    """Manual push / retry: re-trigger todo-push for one ext ref."""
    todo = next((t for t in todo_store.list_todos() if t["id"] == todo_id), None)
    if todo is None:
        return {"ok": False, "error": "not found"}
    pid = (data or {}).get("provider") or ""
    item = next((x for x in (todo.get("ext") or []) if x.get("provider") == pid), None)
    prov = todo_providers.get_todo_provider(pid)
    if not item or not prov:
        return {"ok": False, "error": f"no ext ref for provider: {pid}"}
    ready, why = _provider_ready(prov)
    if not ready:
        return {"ok": False, "error": why}
    run = _trigger_todo_workflow(
        "todo-push",
        prov,
        {"ext_id": str(item["id"]), "title": todo["title"], "url": str(item.get("url") or "")},
    )
    if not run:
        return {"ok": False, "error": "todo-push workflow missing"}
    sync_ledger.append(todo_id, pid, str(item["id"]), "push", run["id"])
    return {"ok": True, "run": run}


@router.delete("/api/todos/{todo_id}")
async def delete_todo_endpoint(todo_id: str) -> dict:
    ok = todo_store.delete_todo(todo_id)
    if ok:
        await _push_global_event("todos.changed", {})
    return {"ok": ok}
