"""Artifacts, uploaded/attached files, and session-files management.

Covers the Artifacts panel (list/delete/metadata/update + ref healing),
composer uploads and drag-in attachments (upload/preview/download/save),
and the Settings → 会话文件 browser (dirs/list/reveal/delete).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

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
    that session (the Artifacts panel does this). Omit for all (back-compat)."""
    return art_store.list_artifacts(project_slug, session_id)


@router.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_endpoint(artifact_id: str, project_slug: str = "default") -> dict:
    """Remove an artifact entry from the panel. The underlying file (if any)
    is NOT touched on disk — deletion is reference-only and recoverable."""
    return {"ok": art_store.delete_artifact(project_slug, artifact_id)}


def _heal_artifact_ref(art: dict, project_slug: str) -> str | None:
    """Re-point an artifact whose file was moved — typical case: generated
    markdown later moved/copied into the knowledge vault — so artifact links
    and previews keep working from the new home instead of showing "file
    missing". Searches the vault by basename/stem; on hit, rewrites the
    artifact ref and relocates (or registers) the file-registry entry.
    Returns the new path, or None when nothing was found."""
    ref = art.get("ref") or ""
    if not ref or Path(ref).is_file():
        return None
    try:
        from ..knowledge.config import load_knowledge_config

        vault = Path(str(load_knowledge_config().vault_path)).expanduser()
        if not vault.is_dir():
            return None
        name, stem = Path(ref).name, Path(ref).stem
        hit = next(
            (c for c in sorted(vault.rglob("*.md")) if c.name == name or c.stem == stem),
            None,
        )
        if hit is None:
            return None
        new = str(hit)
        art_store.set_ref(project_slug, art["id"], new)
        reg = files_mod.get_registry(project_slug)
        old_entry = reg.find_by_path(ref)
        if old_entry:
            reg.relocate(old_entry["id"], new)
        else:
            reg.register(
                hit.name, new, session_id=art.get("session_id"), artifact_id=art["id"]
            )
        return new
    except Exception:
        _log.exception("artifact_ref_heal_failed artifact=%s", art.get("id"))
        return None


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
    """Heal refs stored relative to the session workspace (models echo the
    relative path they passed write_file — see 2026-08 us_stock_report). On
    hit, rewrites the artifact ref to the absolute path and registers the
    file-registry entry — same shape as _heal_artifact_ref, and covers
    records persisted before registration-side normalization existed."""
    ref = (art.get("ref") or "").strip()
    if not ref or Path(ref).is_file():
        return None
    sid = art.get("session_id") or ""
    if not sid:
        return None
    try:
        # Path join with an absolute `ref` yields `ref` itself, which is
        # already known missing — so broken absolute refs heal to nothing
        # here and fall through to the vault healer unchanged.
        candidate = Path(str(_session_workspace(project_slug, sid) / ref)).expanduser()
        if not candidate.is_file():
            return None
        new = files_mod.norm_path(str(candidate))
        art_store.set_ref(project_slug, art["id"], new)
        files_mod.get_registry(project_slug).register(
            art.get("name") or candidate.name, new, session_id=sid, artifact_id=art["id"]
        )
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
async def artifact_metadata_endpoint(artifact_id: str, project_slug: str = "default") -> dict:
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
    if art.get("kind") == "file" and ref:
        reg = files_mod.get_registry(project_slug)
        if not Path(ref).is_file():
            # The ref may be workspace-relative (models echo write_file's
            # relative path) or the file may have moved into the knowledge
            # vault — re-point ref + registry so the link heals instead of
            # breaking. Workspace heal first: it's cheaper and the common case.
            healed = _heal_workspace_ref(art, project_slug) or _heal_artifact_ref(
                art, project_slug
            )
            if healed:
                ref = healed
        file_entry = reg.find_by_path(ref)
        path = (file_entry or {}).get("path") or ref
        exists = Path(path).is_file()
        override = (art.get("schema") or "").strip()
        effective_kind = (file_entry or {}).get("kind") or files_ex.classify(path)
        if override:
            schema, schema_source = override, "override"
        elif effective_kind in ("spreadsheet", "table"):
            schema = _compact_schema(path)
            schema_source = "computed" if schema else ""
    return {
        "ok": True,
        "artifact": art,
        "file": file_entry,
        "exists": exists,
        "schema": schema,
        "schema_source": schema_source,
    }


@router.put("/api/artifacts/{artifact_id}")
async def update_artifact_endpoint(
    artifact_id: str, data: dict, project_slug: str = "default"
) -> dict:
    """User corrections from the metadata inspector. ``name/kind/ref/schema``
    land on the artifact record (``schema`` becomes the injection override);
    ``file_kind`` corrects the registry's classification, which steers the
    prompt's tool guidance (analyze_table vs parse_document)."""
    if art_store.get_artifact(project_slug, artifact_id) is None:
        return {"ok": False, "error": "not found"}
    patch = {k: data[k] for k in ("name", "kind", "ref", "schema") if data.get(k) is not None}
    updated = art_store.update_artifact(project_slug, artifact_id, patch)
    if updated is None:
        return {"ok": False, "error": "名称不能为空"}
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
            "error": f"文件过大（上限 {UPLOAD_MAX_BYTES // 1024 // 1024}MB）",
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
        return {"ok": False, "error": f"文件不存在: {src}"}
    name = p.name
    dest_dir = paths.session_uploads_dir(slug, session_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    try:
        shutil.copyfile(p, dest)
    except OSError as e:
        return {"ok": False, "error": f"无法读取文件: {e}"}
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
        return {"ok": False, "error": f"file not found: {file_id}"}
    if not Path(entry["path"]).is_file() and entry.get("artifact_id"):
        # File may have been moved into the knowledge vault — heal the
        # registry entry (via its artifact) and retry from the new path.
        slug = entry.get("project_slug") or "default"
        art = art_store.get_artifact(slug, entry["artifact_id"])
        if art is not None and _heal_artifact_ref(art, slug):
            entry = files_mod.get_by_id(file_id) or entry
    try:
        payload = files_preview.build_preview(
            entry["path"], sheet=sheet, offset=offset, limit=limit
        )
    except (files_ex.UnsupportedFormat, files_ex.ExtractorUnavailable) as e:
        return {"ok": False, "error": str(e)}
    except FileNotFoundError:
        return {"ok": False, "error": "文件已被移动或删除"}
    except Exception as e:  # parse failure → actionable error, not 500
        return {"ok": False, "error": f"预览失败: {type(e).__name__}: {e}"}
    # a fresh preview counts as "seen": clear the stale badge + sync mtime
    try:
        entry["mtime"] = Path(entry["path"]).stat().st_mtime
    except OSError:
        pass
    entry["stale"] = False
    return {
        "ok": True,
        "file": {
            "id": entry["id"],
            "name": entry["name"],
            "kind": entry.get("kind", ""),
            "path": entry["path"],
            "stale": entry.get("stale", False),
        },
        **payload,
    }


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
        raise HTTPException(status_code=404, detail=f"file not found: {file_id}")
    p = Path(entry["path"])
    if not p.is_file():
        raise HTTPException(status_code=404, detail="文件已被移动或删除")
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
                status_code=500, detail=f"导出失败: {type(e).__name__}: {e}"
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
        return {"ok": False, "error": "文件已被移动或删除"}
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
        return {"ok": False, "error": f"写入 Downloads 失败: {e}"}
    return {"ok": True, "path": str(dest), "name": dest.name}


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
        return {"ok": False, "error": "文件不存在"}
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
        return {"ok": False, "error": f"无法打开文件管理器: {e}"}
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
        return {"ok": False, "error": "仅支持删除已删除会话的文件；请先删除该会话"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_file():
        return {"ok": False, "error": "文件不存在"}
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
        return {"ok": False, "error": f"删除失败: {e}"}
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
        return {"ok": False, "error": "仅支持删除已删除会话的文件；请先删除该会话"}
    target = _session_file_guard(slug, sid, sub)
    if target is None:
        return {"ok": False, "error": "invalid session or path"}
    if not target.is_dir():
        return {"ok": False, "error": "目录不存在"}
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
        return {"ok": False, "error": f"删除失败: {e}"}
    return {"ok": True, "files_removed": removed}
