"""Context folder library endpoints (docs/context-folders-design.md §4.3)."""

from __future__ import annotations

from fastapi import APIRouter

from .. import context_folders as cf

router = APIRouter()


@router.get("/api/folders")
async def list_folders() -> dict:
    return {"ok": True, "folders": cf.load_library()}


@router.post("/api/folders")
async def create_folder(req: dict) -> dict:
    """Register a directory (idempotent on resolved path). Probes first and
    rejects non-directories so the library never holds dead entries."""
    path = str((req or {}).get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "path is required"}
    p = cf.probe(path)
    if not p.get("ok"):
        return {"ok": False, "error": p.get("error") or "invalid path", "probe": p}
    folder = cf.add_folder(
        path,
        name=(req.get("name") or "").strip() or None,
        access=(req.get("access") or cf.DEFAULT_ACCESS),
        load_rules=bool(req.get("load_rules", True)),
    )
    return {"ok": True, "folder": folder, "probe": p}


@router.post("/api/folders/probe")
async def probe_folder(req: dict) -> dict:
    return cf.probe(str((req or {}).get("path") or ""))


@router.patch("/api/folders/{folder_id}")
async def patch_folder(folder_id: str, patch: dict) -> dict:
    folder = cf.update_folder(folder_id, patch or {})
    if folder is None:
        return {"ok": False, "error": "unknown folder"}
    return {"ok": True, "folder": folder}


@router.delete("/api/folders/{folder_id}")
async def delete_folder(folder_id: str) -> dict:
    """Remove from the library. Sessions still referencing the id degrade to
    a ``missing`` marker (design §4.1) — never a crash."""
    removed = cf.remove_folder(folder_id)
    return {"ok": True, "removed": removed}
