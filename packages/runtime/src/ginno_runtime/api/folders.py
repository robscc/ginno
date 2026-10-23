"""Context folder library endpoints (docs/context-folders-design.md §4.3)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import context_folders as cf

router = APIRouter()


@router.get("/api/folders")
async def list_folders() -> dict:
    return {"ok": True, "folders": cf.load_library()}


@router.get("/api/folders/{folder_id}")
async def get_folder(folder_id: str) -> dict:
    """Read one context folder. Unknown id → 404 JSON (the GET used to fall
    through to the SPA catch-all and answer 200 HTML)."""
    folder = cf.get_folder(folder_id)
    if folder is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown folder"}
        )
    return {"ok": True, "folder": folder}


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
        auto_mount=bool(req.get("auto_mount", False)),
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
