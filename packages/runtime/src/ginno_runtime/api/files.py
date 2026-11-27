"""Artifacts, uploaded/attached files, and session-files management.

Covers the Artifacts panel (list/delete/metadata/update + explicit relinking),
composer uploads and drag-in attachments (upload/preview/download/save),
and the Settings → 会话文件 browser (dirs/list/reveal/delete).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ginno_runtime import jsonx as json

from .. import artifacts as art_store
from .. import files as files_mod
from .. import paths
from ..server_shared import _log
from ..session_meta import _resolve_session_meta, _session_meta_list

router = APIRouter()


# ---- artifacts ----


@router.get("/api/artifacts")
async def list_artifacts_endpoint(
    project_slug: str = "default", session_id: str | None = None
) -> list[dict]:
    """Artifacts belong to a session: pass ``session_id`` to scope the list to
    that session (the Artifacts panel does this). Omit for all (back-compat;
    the @ mention menu does this — cross-session picking is the explicit-mention
    consent path, agent file tools' containment is untouched).

    条目附 ``session_title``（来源会话标题，纯展示用——跨会话条目在菜单里
    标注出处）；标题查不到留空。"""
    items = art_store.list_artifacts(project_slug, session_id)
    from ..session_meta import _find_meta

    titles: dict[str, str] = {}
    for a in items:
        sid = a.get("session_id") or ""
        if sid and sid not in titles:
            meta, _ = _find_meta(sid) or ({}, None)
            titles[sid] = (meta or {}).get("title") or ""
    for a in items:
        a["session_title"] = titles.get(a.get("session_id") or "", "")
    return items


@router.get("/api/artifacts/{artifact_id}")
async def get_artifact_endpoint(
    artifact_id: str, project_slug: str = "default"
) -> dict:
    """Read one artifact entry. Unknown id → 404 JSON (without this route the
    GET fell through to the SPA catch-all and returned 200 HTML)."""
    art = art_store.get_artifact(project_slug, artifact_id)
    if art is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown artifact"}
        )
    return art


@router.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_endpoint(artifact_id: str, project_slug: str = "default") -> dict:
    """Remove an artifact entry from the panel. The underlying file (if any)
    is NOT touched on disk — deletion is reference-only and recoverable."""
    return {"ok": art_store.delete_artifact(project_slug, artifact_id)}


def _artifact_relink_candidates(ref: str) -> list[str]:
    """Offer exact-name matches for the user to inspect and select."""
    from ..knowledge.config import load_knowledge_config

    vault_path = load_knowledge_config().vault_path
    if not vault_path:
        return []
    vault = Path(vault_path).expanduser()
    if not vault.is_dir():
        return []
    name = Path(ref).name
    return sorted({
        files_mod.norm_path(p) for p in vault.rglob("*") if p.name == name and p.is_file()
    })


def _session_workspace(slug: str, session_id: str) -> Path:
    """The session's authoritative files dir — where write_file's relative
    paths land. Prefers the recorded meta workspace; falls back to the
    standard layout for sessions whose meta is stale."""
    meta = _resolve_session_meta(session_id)
    ws = str((meta or {}).get("workspace") or "")
    return Path(ws) if ws else paths.session_files_dir(slug, session_id)


def _normalize_file_ref(slug: str, session_id: str, ref: str) -> str:
    """Resolve a model-supplied file ref to an absolute path when possible.

    Models echo back the same (often relative) path they passed write_file;
    those resolve against the session workspace, not the sidecar cwd. Only
    refs that point at an existing file are rewritten — anything else (link
    URLs, docs, missing files) passes through untouched.
    """
    ref = (ref or "").strip()
    if not ref:
        return ref
    p = Path(ref).expanduser()
    if not p.is_absolute():
        p = Path(str(_session_workspace(slug, session_id) / ref)).expanduser()
    if p.is_file():
        return files_mod.norm_path(str(p))
    return ref


def _register_artifact_file(slug: str, session_id: str, art: dict, ref: str) -> None:
    """Give an artifact's on-disk file a registry entry (idempotent) so
    preview / download / injection can find it by path. Without this the
    panel row exists but clicking it resolves nothing (files.json miss)."""
    if not art or not ref or not Path(ref).is_file():
        return
    files_mod.get_registry(slug).register(
        art.get("name") or Path(ref).name,
        ref,
        session_id=session_id,
        artifact_id=art.get("id"),
    )


def _heal_workspace_ref(art: dict, project_slug: str) -> str | None:
    """Resolve a legacy relative ref against its recorded workspace."""
    ref = (art.get("ref") or "").strip()
    sid = art.get("session_id") or ""
    if not ref or not sid or art.get("origin") == "remote" or Path(ref).expanduser().is_absolute():
        return None
    try:
        candidate = _session_workspace(project_slug, sid) / ref
        if not candidate.is_file():
            return None
        new = files_mod.norm_path(candidate)
        reg = files_mod.get_registry(project_slug)
        if not reg.find_by_path(new):
            reg.register(art.get("name") or candidate.name, new, session_id=sid, artifact_id=art["id"])
        art_store.set_ref(project_slug, art["id"], new)
        return new
    except Exception:
        _log.exception("artifact_workspace_heal_failed artifact=%s", art.get("id"))
        return None


def _compact_schema(path: str) -> str:
    """One-line schema summary for prompt injection (tables only, best-effort)."""
    try:
        from ..files import extractors as _ex

        s = _ex.schema_summary(path, sample_rows=2)
        bits = []
        for sh in s.get("sheets", [])[:3]:
            cols = ", ".join(
                f"{c['name']}({c['dtype']})" for c in sh.get("columns", [])[:12]
            )
            bits.append(
                f"[{sh['name']}] {sh['rows']}行×{sh['cols']}列, 列: {cols}"
                + (f", 样例: {sh['sample']}" if sh.get("sample") else "")
            )
        return "; ".join(bits)[:500]
    except Exception:
        return ""


@router.get("/api/artifacts/{artifact_id}/metadata")
async def artifact_metadata_endpoint(
    artifact_id: str, project_slug: str = "default", candidates: bool = False,
    include_schema: bool = True,
) -> dict:
    """Full inspector payload for one artifact: the panel record, its file
    registry entry (size/mtime/kind), whether the file still exists on disk,
    and the EXACT schema summary that prompt injection would use — with its
    provenance (user override vs auto-computed) so the UI can show both."""
    from ..files import extractors as files_ex

    art = art_store.get_artifact(project_slug, artifact_id)
    if art is None:
        return {"ok": False, "error": "not found"}
    file_entry = None
    exists = False
    schema = ""
    schema_source = ""
    ref = art.get("ref") or ""
    local_file = art.get("kind") in ("file", "image") and art.get("origin") != "remote"
    if local_file and ref:
        # Relative legacy refs use their exact workspace path. A missing file
        # at a known path has no implied successor.
        if healed := _heal_workspace_ref(art, project_slug):
            ref = healed
            art = {**art, "ref": healed}
        resolved = Path(ref).expanduser()
        if not resolved.is_absolute() and art.get("session_id"):
            resolved = _session_workspace(project_slug, art["session_id"]) / resolved
        if resolved.is_absolute():
            file_entry = files_mod.get_registry(project_slug).find_by_path(resolved)
        path = (file_entry or {}).get("path") or str(resolved)
        exists = resolved.is_absolute() and resolved.is_file()
        if include_schema and art.get("kind") == "file":
            override = (art.get("schema") or "").strip()
            effective_kind = (file_entry or {}).get("kind") or files_ex.classify(path)
            if override:
                schema, schema_source = override, "override"
            elif exists and effective_kind in ("spreadsheet", "table"):
                schema = _compact_schema(path)
                schema_source = "computed" if schema else ""
    paths_to_choose = []
    if candidates and local_file and ref and not exists:
        try:
            paths_to_choose = await asyncio.to_thread(_artifact_relink_candidates, ref)
        except (OSError, ValueError):
            _log.exception("artifact_candidates_failed artifact=%s", artifact_id)
    return {
        "ok": True,
        "artifact": art,
        "file": file_entry,
        "exists": exists,
        "schema": schema,
        "schema_source": schema_source,
        "relink_candidates": paths_to_choose,
    }


def _relink_artifact(project_slug: str, art: dict, selection: Any) -> dict | JSONResponse:
    def rejected(status: int, error: str) -> JSONResponse:
        return JSONResponse(status_code=status, content={"ok": False, "error": error})

    ref = art.get("ref") or ""
    if art.get("kind") not in ("file", "image") or art.get("origin") == "remote" or not ref:
        return rejected(409, "relink_conflict")
    if not isinstance(selection, dict) or not isinstance(selection.get("path"), str):
        return rejected(400, "relink_invalid_target")
    reg = files_mod.get_registry(project_slug)
    old_path = Path(ref).expanduser()
    if not old_path.is_absolute():
        old_path = _session_workspace(project_slug, art.get("session_id") or "") / old_path
    entry = reg.find_by_path(old_path)
    file_id = (entry or {}).get("id")
    if (
        selection.get("expected_ref") != ref
        or "expected_file_id" not in selection
        or selection["expected_file_id"] != file_id
        or old_path.is_file()
        or (entry and (entry.get("project_slug") != project_slug
                       or entry.get("artifact_id") not in (None, "", art["id"])))
        or any(e.get("artifact_id") == art["id"] and e["id"] != file_id for e in reg.list_all())
    ):
        return rejected(409, "relink_conflict")

    target = Path(selection["path"])
    try:
        if not target.is_absolute():
            return rejected(400, "relink_invalid_target")
        new = files_mod.norm_path(target)
        if not Path(new).is_file():
            return rejected(404, "relink_invalid_target")
    except (OSError, ValueError, RuntimeError):
        return rejected(404, "relink_invalid_target")
    if reg.find_by_path(new) or any(
        a["id"] != art["id"] and a.get("origin") != "remote"
        and a.get("kind") in ("file", "image")
        and a.get("ref") and Path(a["ref"]).expanduser().is_absolute()
        and files_mod.norm_path(a["ref"]) == new
        for a in art_store.list_artifacts(project_slug)
    ):
        return rejected(409, "relink_target_in_use")

    updated = art_store.set_ref(project_slug, art["id"], new)
    try:
        if file_id:
            reg.relocate(file_id, new, artifact_id=art["id"])
        else:
            reg.register(art["name"], new, session_id=art.get("session_id") or "", artifact_id=art["id"], require_file=True)
    except OSError as exc:
        art_store.set_ref(project_slug, art["id"], ref)
        if isinstance(exc, FileNotFoundError):
            return rejected(404, "relink_invalid_target")
        if isinstance(exc, FileExistsError):
            return rejected(409, "relink_target_in_use")
        _log.exception("artifact_relink_failed artifact=%s", art["id"])
        return rejected(500, "relink_failed")
    return {"ok": True, "artifact": updated}


@router.put("/api/artifacts/{artifact_id}")
async def update_artifact_endpoint(
    artifact_id: str, data: dict, project_slug: str = "default"
) -> dict:
    """User corrections from the metadata inspector. ``name/kind/ref/schema``
    land on the artifact record (``schema`` becomes the injection override);
    ``file_kind`` corrects the registry's classification, which steers the
    prompt's tool guidance (schema summary shown for tables)."""
    art = art_store.get_artifact(project_slug, artifact_id)
    if art is None:
        return {"ok": False, "error": "not found"}
    if "relink" in data:
        return _relink_artifact(project_slug, art, data["relink"])
    patch = {k: data[k] for k in ("name", "kind", "ref", "schema") if data.get(k) is not None}
    updated = art_store.update_artifact(project_slug, artifact_id, patch)
    if updated is None:
        return {"ok": False, "error": "Name must not be empty"}
    file_kind = (data.get("file_kind") or "").strip()
    if file_kind and updated.get("kind") == "file" and updated.get("ref"):
        files_mod.get_registry(project_slug).set_kind(updated["ref"], file_kind)
    return {"ok": True, "artifact": updated}


# ---- files (upload / preview — see docs/file-parsing-research.md §7) ----
UPLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_UNSAFE_NAME_RE = re.compile(r"[^\w一-鿿.\-]+", re.UNICODE)


def _safe_upload_name(name: str) -> str:
    base = Path(name).name
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip("._")
    return cleaned or "file"


@router.post("/api/files")
async def upload_file_endpoint(
    session_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> dict:
    """Upload a file attached in the composer; lands in the session's files dir
    under ``uploads/`` and becomes a ``kind=file`` artifact (session-scoped)."""
    meta = _resolve_session_meta(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    slug = meta.get("project_slug") or "default"
    name = _safe_upload_name(file.filename or "file")
    data = await file.read()
    if len(data) > UPLOAD_MAX_BYTES:
        return {
            "ok": False,
            # code 让前端可靠区分「超限拒绝」与其它失败（error 文本仅展示用）
            "code": "file_too_large",
            "error": f"File too large (max {UPLOAD_MAX_BYTES // 1024 // 1024}MB)",
        }
    dest_dir = paths.session_uploads_dir(slug, session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    dest.write_bytes(data)
    # ref must match the registry's normalized path (symlink-safe), or the
    # UI's artifact → preview/download lookup by path misses (macOS /tmp).
    art = art_store.add_artifact(slug, "file", name, files_mod.norm_path(dest), session_id)
    entry = files_mod.get_registry(slug).register(
        name,
        dest,
        mime=file.content_type or "",
        size=len(data),
        session_id=session_id,
        artifact_id=art.get("id"),
    )
    return {"ok": True, "file": entry}


@router.get("/api/files")
async def list_files_endpoint(
    project_slug: str = "default", session_id: str | None = None
) -> list[dict]:
    reg = files_mod.get_registry(project_slug)
    return reg.list_session(session_id) if session_id else reg.list_all()


@router.post("/api/files/attach-path")
async def attach_file_by_path_endpoint(req: dict) -> dict:
    """Attach an OS file the user dragged into the desktop app.

    WKWebView can't expose dropped files to JS, so the Tauri shell forwards the
    native path here; the sidecar (same filesystem) copies it into the session's
    files dir and registers it like an upload.
    """
    import shutil

    from ..files import extractors as files_ex

    session_id = req.get("session_id") or ""
    src = req.get("path") or ""
    meta = _resolve_session_meta(session_id)
    if meta is None:
        return {"ok": False, "error": f"session not found: {session_id}"}
    slug = meta.get("project_slug") or "default"
    p = Path(src).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"File not found: {src}"}
    # Q2（R3 巡检）：与 upload 端点同一上限——原生拖拽（Tauri shell 转发
    # 路径）此前无大小检查直接 copyfile，绕过了 100MB 限额。同一常量、
    # 同一错误码口径，前端据此给与上传一致的可见报错（响应带实际 size，
    # 文案需要呈现文件大小）。
    src_size = p.stat().st_size
    if src_size > UPLOAD_MAX_BYTES:
        return {
            "ok": False,
            "code": "file_too_large",
            "size": src_size,
            "error": f"File too large (max {UPLOAD_MAX_BYTES // 1024 // 1024}MB)",
        }
    name = p.name
    dest_dir = paths.session_uploads_dir(slug, session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    try:
        shutil.copyfile(p, dest)
    except OSError as e:
        return {"ok": False, "error": f"Failed to read file: {e}"}
    kind = files_ex.classify(dest)
    art = art_store.add_artifact(slug, "file", name, files_mod.norm_path(dest), session_id)
    entry = files_mod.get_registry(slug).register(
        name,
        dest,
        kind=kind,
        size=dest.stat().st_size,
        session_id=session_id,
        artifact_id=art.get("id"),
    )
    return {"ok": True, "file": entry}


@router.post("/api/files/ensure-registered")
async def ensure_registered_endpoint(req: dict) -> dict:
    """Give a model-attached file ref (attach_ref 的 ref_id) a registry entry.

    attach_ref 的 ref chip 在 UI 里只是展示，除非路径能解析到文件注册表的
    id；live 转向在 ref.emit 时注册（stream engine），但老会话历史回放出的
    ref 从未注册过。chip 点击时调用这里：路径按模型 ref 的同一规则归一化
    （相对路径 → session workspace），文件存在则原地登记（不拷贝，幂等）。
    """
    session_id = req.get("session_id") or ""
    src = req.get("path") or ""
    meta = _resolve_session_meta(session_id)
    if meta is None:
        return {"ok": False, "code": "session_not_found", "error": f"session not found: {session_id}"}
    slug = meta.get("project_slug") or "default"
    ref = _normalize_file_ref(slug, session_id, src)
    p = Path(ref).expanduser()
    if not p.is_file():
        return {"ok": False, "code": "file_not_found", "error": f"File not found: {src}"}
    entry = files_mod.get_registry(slug).register(
        req.get("name") or p.name, ref, session_id=session_id
    )
    return {"ok": True, "file": entry}


@router.post("/api/debug-log")
async def debug_log_endpoint(data: dict) -> dict:
    """Temporary: frontend drop/upload telemetry for diagnosing WKWebView DnD."""
    # print (not _log) — stdout is forwarded to sidecar.log by the Tauri shell;
    # the Python logger isn't wired to a visible sink in the frozen build.
    print("DEBUG-DROP " + json.dumps(data, ensure_ascii=False, default=str), flush=True)
    return {"ok": True}


@router.get("/api/files/{file_id}/preview")
async def file_preview_endpoint(
    file_id: str,
    sheet: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Paginated grid for spreadsheets/tables; extracted markdown for docs."""
    from ..files import extractors as files_ex
    from ..files import preview as files_preview

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        # Same contract as /download: missing file is a 404, and the body is
        # JSON either way (the old 200 + ok:false made preview and download
        # disagree on the status code for the identical failure).
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"file not found: {file_id}"},
        )
    path = entry["path"]
    try:
        payload = await asyncio.to_thread(
            files_preview.build_preview, path, sheet=sheet, offset=offset, limit=limit
        )
    except (files_ex.UnsupportedFormat, files_ex.ExtractorUnavailable) as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": "File has been moved or deleted"},
        )
    except Exception as e:  # parse failure → actionable error, not 500
        return {"ok": False, "error": f"Preview failed: {type(e).__name__}: {e}"}
    # A fresh preview counts as seen unless the registry moved to another path
    # while the worker was reading this one.
    if entry["path"] == path:
        try:
            entry["mtime"] = Path(path).stat().st_mtime
        except OSError:
            pass
        entry["stale"] = False
    return {
        "ok": True,
        "file": {
            "id": entry["id"],
            "name": entry["name"],
            "kind": entry.get("kind", ""),
            "path": path,
            "stale": entry.get("stale", False),
        },
        **payload,
    }


_CSP_SANDBOX = (
    "sandbox allow-scripts; "
    "default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval'; "
    "connect-src 'none'; form-action 'none'"
)


@router.get("/api/files/{file_id}/inline")
async def file_inline_endpoint(file_id: str) -> Any:
    """Serve an html file for in-app iframe preview — the SheetViewer twin of
    the code panel's HtmlPreview. Sandboxed twice on purpose: the CSP header
    here (opaque origin, no network) AND the iframe attribute in the client.
    No Content-Disposition so the browser renders instead of downloading."""
    from starlette.responses import Response as _Resp

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": f"file not found: {file_id}"}
        )
    p = Path(entry["path"])
    if not p.is_file():
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "File has been moved or deleted"}
        )
    if entry.get("kind") != "html":
        raise HTTPException(
            status_code=415,
            detail={"ok": False, "error": f"not previewable inline: {entry.get('kind')}"},
        )
    try:
        body = await asyncio.to_thread(p.read_bytes)
    except OSError as e:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "File has been moved or deleted"}
        ) from e
    return _Resp(
        content=body,
        media_type="text/html; charset=utf-8",
        headers={"Content-Security-Policy": _CSP_SANDBOX, "Cache-Control": "no-store"},
    )


def _attachment_headers(filename: str) -> dict[str, str]:
    """Content-Disposition for downloads; RFC 5987 filename* for UTF-8 names."""
    from urllib.parse import quote

    fallback = filename.encode("ascii", "replace").decode().replace('"', "_")
    return {
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'
        )
    }


@router.get("/api/files/{file_id}/download")
async def file_download_endpoint(
    file_id: str,
    fmt: str = "raw",
    sheet: str | None = None,
) -> Any:
    """Download the original file (fmt=raw) or export one sheet as CSV
    (fmt=csv — spreadsheet/table kinds only; ``sheet`` selects which)."""
    from starlette.responses import Response

    from ..files import extractors as files_ex
    from ..files import preview as files_preview

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": f"file not found: {file_id}"},
        )
    p = Path(entry["path"])
    if not p.is_file():
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": "File has been moved or deleted"},
        )
    if fmt == "raw":
        from starlette.responses import FileResponse

        return FileResponse(
            p,
            filename=entry.get("name") or p.name,
            headers=_attachment_headers(entry.get("name") or p.name),
        )
    if fmt == "csv":
        try:
            name, data = files_preview.build_csv_export(
                p, sheet=sheet, name=entry.get("name")
            )
        except files_ex.UnsupportedFormat as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except files_ex.ExtractorUnavailable as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        except Exception as e:  # parse failure → actionable error, not bare 500
            raise HTTPException(
                status_code=500, detail=f"Export failed: {type(e).__name__}: {e}"
            ) from e
        return Response(
            content=data,
            media_type="text/csv; charset=utf-8",
            headers=_attachment_headers(name),
        )
    raise HTTPException(status_code=400, detail=f"unsupported fmt: {fmt}")


# Collision-free destination naming lives in files.registry (shared with the
# session-files relocation / migration paths).
_unique_dest = files_mod.unique_dest


@router.post("/api/files/{file_id}/save-to-downloads")
async def save_file_to_downloads_endpoint(
    file_id: str, req: dict | None = None
) -> dict:
    """Copy the file (or a CSV export of it) into the OS Downloads folder.

    WKWebView can't trigger browser downloads, so the desktop UI calls this
    instead: the sidecar (same filesystem, same user) writes the copy and
    reports the destination path. Body: ``{"fmt": "raw"|"csv", "sheet"?}``.
    """
    import os
    import shutil

    from ..files import extractors as files_ex
    from ..files import preview as files_preview

    req = req or {}
    fmt = req.get("fmt") or "raw"
    sheet = req.get("sheet")
    entry = files_mod.get_by_id(file_id)
    if entry is None:
        return {"ok": False, "error": f"file not found: {file_id}"}
    p = Path(entry["path"])
    if not p.is_file():
        return {"ok": False, "error": "File has been moved or deleted"}
    downloads = Path(os.environ.get("GINNO_DOWNLOADS") or (Path.home() / "Downloads"))
    try:
        downloads.mkdir(parents=True, exist_ok=True)
        if fmt == "raw":
            name = entry.get("name") or p.name
            dest = _unique_dest(downloads / name)
            shutil.copyfile(p, dest)
        elif fmt == "csv":
            name, data = files_preview.build_csv_export(
                p, sheet=sheet, name=entry.get("name")
            )
            dest = _unique_dest(downloads / name)
            dest.write_bytes(data)
        else:
            return {"ok": False, "error": f"unsupported fmt: {fmt}"}
    except files_ex.UnsupportedFormat as e:
        return {"ok": False, "error": str(e)}
    except files_ex.ExtractorUnavailable as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": f"Failed to write to Downloads: {e}"}
    return {"ok": True, "path": str(dest), "name": dest.name}


@router.post("/api/files/{file_id}/open-external")
async def open_file_external_endpoint(file_id: str) -> dict:
    """Open the file with the OS default application (Preview for PDFs,
    Excel/Numbers for spreadsheets, etc.). Best-effort: returns ok=False when
    the file is missing or the OS launcher fails to start."""
    import subprocess
    import sys

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        return {"ok": False, "error": f"file not found: {file_id}"}
    p = Path(entry["path"])
    if not p.is_file():
        return {"ok": False, "error": "File has been moved or deleted"}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError as e:
        return {"ok": False, "error": f"Failed to launch external app: {e}"}
    return {"ok": True}


@router.post("/api/files/{file_id}/reveal")
async def reveal_file_endpoint(file_id: str) -> dict:
    """Reveal the file in the OS file manager (Finder / Explorer / xdg).

    与 open-external 的区别：不打开文件本体，只定位——大产物（drawio/
    xlsx/报告）经常需要「去所在目录看看还有什么」。Best-effort。"""
    import subprocess
    import sys

    entry = files_mod.get_by_id(file_id)
    if entry is None:
        return {"ok": False, "error": f"file not found: {file_id}"}
    p = Path(entry["path"])
    if not p.exists():
        return {"ok": False, "error": "File has been moved or deleted"}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            # Linux 无统一 select 语义：打开所在目录
            subprocess.Popen(["xdg-open", str(p.parent)])
    except OSError as e:
        return {"ok": False, "error": f"Failed to open file manager: {e}"}
    return {"ok": True}


# ---- session files management (Settings → 会话文件) ----
def _session_file_guard(slug: str, session_id: str, sub: str | None) -> Path | None:
    """Resolve ``sub`` (a path relative to the session dir) and require it to
    stay inside ``sessions/<session_id>/``. Returns the resolved Path, or None
    if the session_id/sub is malformed or escapes the dir (path traversal)."""
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return None
    base = paths.session_files_dir(slug, session_id).resolve()
    if not sub:
        return base
    target = (base / sub).resolve()
    try:
        if not target.is_relative_to(base):
            return None
    except ValueError:
        return None
    return target


def _is_orphaned_session(slug: str, session_id: str) -> bool:
    """True when the session no longer exists in the project's session index.

    Deletion of session files is restricted to orphaned sessions: an active
    session's files are "live" (in use by the conversation), so they can be
    browsed/revealed but not removed from Settings. Only once the session itself
    is deleted do its preserved files become cleanable.
    """
    if not session_id:
        return False
    return not any(m.get("id") == session_id for m in _session_meta_list(slug))


def _dir_stats(d: Path) -> tuple[int, int, float]:
    """(file_count, total_bytes, newest mtime) for a directory tree."""
    n = 0
    size = 0
    mtime = 0.0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                n += 1
                try:
                    st = f.stat()
                    size += st.st_size
                    mtime = max(mtime, st.st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return n, size, mtime


@router.get("/api/session-files/dirs")
async def list_session_file_dirs_endpoint() -> dict:
    """List every per-session files directory across all projects, including
    orphaned ones (session deleted but its files preserved)."""
    out: list[dict] = []
    projects_root = paths.home() / "projects"
    if projects_root.is_dir():
        for proj in sorted(projects_root.iterdir()):
            if not proj.is_dir():
                continue
            slug = proj.name
            sessions_root = paths.project_sessions_dir(slug)
            if not sessions_root.is_dir():
                continue
            metas = {m.get("id"): m for m in _session_meta_list(slug)}
            for d in sorted(sessions_root.iterdir()):
                if not d.is_dir():  # skips _index.json and <sid>.json checkpoints
                    continue
                sid = d.name
                meta = metas.get(sid)
                n, size, mtime = _dir_stats(d)
                out.append(
                    {
                        "project_slug": slug,
                        "session_id": sid,
                        "title": (meta or {}).get("title"),
                        "orphaned": meta is None,
                        "dir": str(d),
                        "file_count": n,
                        "total_bytes": size,
                        "mtime": mtime,
                    }
                )
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"ok": True, "sessions": out}


@router.get("/api/session-files/list")
async def list_session_files_endpoint(
    project_slug: str = "default", session_id: str = "", sub: str | None = None
) -> dict:
    target = _session_file_guard(project_slug, session_id, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_dir():
        return {"ok": True, "path": str(target), "entries": []}
    entries = []
    for child in target.iterdir():
        if child.name.startswith("."):
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "size": st.st_size if child.is_file() else 0,
                "mtime": st.st_mtime,
            }
        )
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    base = paths.session_files_dir(project_slug, session_id).resolve()
    rel = str(target.relative_to(base)) if target != base else ""
    return {"ok": True, "path": rel, "entries": entries}


@router.post("/api/session-files/reveal")
async def reveal_session_file_endpoint(req: dict) -> dict:
    """Reveal a session file/dir in the OS file manager (Finder on macOS)."""
    import subprocess
    import sys

    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    target = _session_file_guard(slug, sid, sub)
    if target is None or not target.exists():
        return {"ok": False, "error": "File not found"}
    try:
        if sys.platform == "darwin":
            if target.is_dir():
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["open", "-R", str(target)])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", f"/select,{target}"])
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except OSError as e:
        return {"ok": False, "error": f"Failed to open file manager: {e}"}
    return {"ok": True}


@router.delete("/api/session-files/file")
async def delete_session_file_endpoint(req: dict) -> dict:
    """Delete one file inside a session dir; also drops its registry entry and
    artifact panel row so the UI reflects the removal.

    Only files of an ORPHANED session (already deleted) can be removed — an
    active session's files are live and protected.
    """
    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    if not _is_orphaned_session(slug, sid):
        return {"ok": False, "error": "Only files of deleted sessions can be removed here; delete the session first"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_file():
        return {"ok": False, "error": "File not found"}
    reg = files_mod.get_registry(slug)
    entry = reg.find_by_path(target)
    unregistered = False
    if entry is not None:
        art_id = entry.get("artifact_id")
        reg.unregister(entry["id"])
        if art_id:
            art_store.delete_artifact(slug, art_id)
        unregistered = True
    try:
        target.unlink()
    except OSError as e:
        return {"ok": False, "error": f"Failed to delete: {e}"}
    return {"ok": True, "unregistered": unregistered}


@router.delete("/api/session-files/dir")
async def delete_session_dir_endpoint(req: dict) -> dict:
    """Delete a subdirectory (or the whole session dir when ``path`` is omitted).
    Purges registry + artifact rows for the files inside first.

    Only an ORPHANED session's directory can be removed — an active session's
    files are live and protected.
    """
    import shutil

    slug = (req or {}).get("project_slug") or "default"
    sid = (req or {}).get("session_id") or ""
    sub = (req or {}).get("path") or ""
    if not _is_orphaned_session(slug, sid):
        return {"ok": False, "error": "Only files of deleted sessions can be removed here; delete the session first"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_dir():
        return {"ok": False, "error": "Directory not found"}
    reg = files_mod.get_registry(slug)
    removed = 0
    prefix = str(target.resolve())
    for e in reg.list_all():
        p = e.get("path") or ""
        if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix + "\\"):
            art_id = e.get("artifact_id")
            reg.unregister(e["id"])
            if art_id:
                art_store.delete_artifact(slug, art_id)
            removed += 1
    try:
        shutil.rmtree(target)
    except OSError as e:
        return {"ok": False, "error": f"Failed to delete: {e}"}
    return {"ok": True, "files_removed": removed}


@router.post("/api/session-files/cleanup-orphans")
async def cleanup_orphan_session_dirs_endpoint() -> dict:
    """One-click cleanup (2026-10-21): remove EVERY orphaned session files dir
    across all projects — sessions deleted but whose preserved files linger.
    Same semantics as ``/dir`` per session (registry + artifact purge first);
    an active session's dir is never touched. Per-dir failures are reported
    and skipped, not fatal — the rest still cleans."""
    import shutil

    removed: list[dict] = []
    failed: list[dict] = []
    freed_bytes = 0
    projects_root = paths.home() / "projects"
    if not projects_root.is_dir():
        return {"ok": True, "removed": [], "failed": [], "freed_bytes": 0}
    for proj in sorted(projects_root.iterdir()):
        if not proj.is_dir():
            continue
        slug = proj.name
        sessions_root = paths.project_sessions_dir(slug)
        if not sessions_root.is_dir():
            continue
        for d in sorted(sessions_root.iterdir()):
            if not d.is_dir():  # _index.json / checkpoints
                continue
            sid = d.name
            if not _is_orphaned_session(slug, sid):
                continue
            _n, size, _mt = _dir_stats(d)
            reg = files_mod.get_registry(slug)
            purged = 0
            prefix = str(d.resolve())
            for e in reg.list_all():
                p = e.get("path") or ""
                if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix + "\\"):
                    art_id = e.get("artifact_id")
                    reg.unregister(e["id"])
                    if art_id:
                        art_store.delete_artifact(slug, art_id)
                    purged += 1
            try:
                shutil.rmtree(d)
                freed_bytes += size
                removed.append({"project_slug": slug, "session_id": sid, "bytes": size})
            except OSError as e:
                failed.append(
                    {"project_slug": slug, "session_id": sid, "error": f"{type(e).__name__}: {e}"[:200]}
                )
    return {
        "ok": len(failed) == 0,
        "removed": removed,
        "failed": failed,
        "freed_bytes": freed_bytes,
    }
