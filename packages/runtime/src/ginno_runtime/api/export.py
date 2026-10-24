"""Data export (V2 P1-3): a session as readable Markdown, a project as a zip.

Everything Ginno knows lives under ``~/.ginno``; without an export path the
user cannot back it up or move it. Two shapes, deliberately simple:

* ``GET /api/export/session/{id}?format=md`` — the conversation as Markdown
  (tool calls collapsed to one line, tool output inside a details block).
* ``POST /api/export/project`` — a zip of the project directory (sessions,
  artifacts, uploads). Caches/indexes are excluded.
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from ginno_runtime import jsonx as json

from .. import paths
from ..session_meta import _find_meta

router = APIRouter()

_MAX_TOOL_TEXT = 4000


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                t = b.get("text") or b.get("thinking") or ""
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def _render_markdown(slug: str, session_id: str, meta: dict) -> str:
    from ..checkpointer import FileCheckpointer

    tup = FileCheckpointer(slug).get_tuple(
        {"configurable": {"thread_id": session_id}}
    )
    messages = (
        (tup.checkpoint.get("channel_values") or {}).get("messages") if tup else []
    ) or []
    title = str(meta.get("title") or session_id)
    lines = [
        f"# {title}",
        "",
        f"- 会话：`{session_id}`",
        f"- 创建：{time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('created') or 0))}",
        f"- 模型：{meta.get('provider') or ''} / {meta.get('model') or ''}",
        "",
        "---",
        "",
    ]
    names = {}
    for m in messages:
        mt = getattr(m, "type", "")
        text = _text(getattr(m, "content", "")).strip()
        if mt == "human":
            if text.startswith(("[turn context]", "[conversation summary]", "[world state")):
                continue
            lines += ["## 用户", "", text, ""]
        elif mt == "ai":
            tool_calls = getattr(m, "tool_calls", None) or []
            if text:
                lines += ["## 助手", "", text, ""]
            for tc in tool_calls:
                names[tc.get("id")] = tc.get("name") or "tool"
                args = json.dumps(tc.get("args") or {}, ensure_ascii=False)[:300]
                lines.append(f"- 🔧 `{tc.get('name')}` {args}")
            if tool_calls:
                lines.append("")
        elif mt == "tool":
            name = names.get(getattr(m, "tool_call_id", ""), "tool")
            body = text[:_MAX_TOOL_TEXT]
            lines += [
                f"<details><summary>🔧 {name} 输出</summary>",
                "",
                "```",
                body,
                "```",
                "",
                "</details>",
                "",
            ]
    return "\n".join(lines)


@router.get("/api/export/session/{session_id}")
async def export_session(
    session_id: str, format: str = Query("md"), project_slug: str | None = None
) -> PlainTextResponse:
    found = _find_meta(session_id)
    if not found:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    meta, slug = found
    if project_slug and slug != project_slug:
        slug = project_slug
    if format not in ("md", "markdown"):
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": f"unsupported format: {format}"}
        )
    md = _render_markdown(slug, session_id, meta)
    name = (str(meta.get("title") or session_id)[:60] or session_id).replace("/", "_")
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.md"
        },
    )


@router.post("/api/export/project")
async def export_project(body: dict | None = None) -> FileResponse:
    """Zip the project: sessions (+world side-cars), artifacts, files registry.

    Excludes caches and the session-file giant blobs only if they live outside
    the project dir — everything inside ``projects/<slug>`` is user data and is
    included."""
    slug = str((body or {}).get("project_slug") or "default")
    src = paths.home() / "projects" / slug
    if not src.is_dir():
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown project"}
        )
    out_dir = paths.home() / "index" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{slug}-{stamp}.zip"
    tmp = out.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if not p.is_file():
                continue
            # Skip per-session checkpoint side-cars of the type only used for
            # live UI state? They are small; keep everything for fidelity.
            arc = Path(slug) / p.relative_to(src)
            try:
                zf.write(p, str(arc))
            except OSError:
                continue
        manifest = {
            "project_slug": slug,
            "exported_at": time.time(),
            "source": str(src),
        }
        zf.writestr(f"{slug}/EXPORT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    tmp.replace(out)
    return FileResponse(
        out,
        media_type="application/zip",
        filename=f"{slug}-{stamp}.zip",
    )
