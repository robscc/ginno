"""Code panel — byte-stream endpoints: raw files + the preview file server.

Split from the former single-module ``api/code.py``;面板总述见
``api/code/__init__.py``。两个端点都为「无法消费 JSON/文本」的消费方服务:
``raw`` 给 ``<img src>``,``file`` 给 HTML sandbox iframe 的路径形态子资源。
"""

from __future__ import annotations

import re

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ._common import (
    CodeAccessError,
    _session_ctx,
    resolve_code_target,
    router,
)


# ---- raw bytes (for surfaces that cannot consume text) ---------------------

#: Ceiling for streaming a file's bytes whole. Same order of magnitude as the
#: read path's preview caps: past this we refuse rather than feed the webview a
#: payload it would choke on.
RAW_MAX_BYTES = 32 * 1024 * 1024

#: ext -> Content-Type for the raw stream. A whitelist on purpose: stdlib
#: ``mimetypes`` consults an OS database and varies by machine, and the one
#: thing an inline stream is for here is images. Keys mirror
#: ``files.extractors.IMAGE_EXTS``; anything else is served as
#: ``application/octet-stream`` with nosniff + an attachment disposition.
_RAW_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    # image/svg+xml so an <img> renders it inertly — SVG loaded through <img>
    # cannot run script against this origin. We deliberately do NOT offer an
    # inline/iframe surface (design §1.3 rules out HTML sandboxing).
    ".svg": "image/svg+xml",
}


@router.get("/api/code/raw")
async def code_raw(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    path: str = "",
) -> Response:
    """A file's bytes, for surfaces that cannot consume text (image preview).

    Runs the SAME fence as ``read`` (design §4.6) — a path the panel may not
    read is a path it may not stream either. The URL is cache-busted with the
    read response's ``version``, so the client builds it as
    ``/api/code/raw?...&v=<version>``.
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _raw_error("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        target, _root_info = resolve_code_target(ctx, root, path, write=False)
    except CodeAccessError as e:
        return _raw_error(e.code, e.message)
    if not target.is_file():
        return _raw_error("not-text", f"Not a file: {path!r}")
    try:
        size = target.stat().st_size
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")
    if size > RAW_MAX_BYTES:
        return _raw_error("too-large", f"File too large ({size / 1048576:.1f} MB) to preview")
    try:
        data = target.read_bytes()
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")

    mime = _RAW_MIME.get(target.suffix.lower())
    headers = {
        # A workspace file must never talk the webview into treating it as
        # markup: octet-stream stays octet-stream.
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if mime is None:
        headers["Content-Disposition"] = "attachment"
    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


def _raw_error(code: str, message: str) -> JSONResponse:
    """Failure as a non-2xx.

    ``raw`` is consumed by ``<img src>``, which cannot read a JSON body: a 200
    carrying ``{ok:false}`` would render as a corrupt image rather than as a
    failure. The code also travels in ``X-Ginno-Code`` so a caller that cares
    can say why.
    """
    status = {
        "unknown-root": 404,
        "root-missing": 404,
        "not-text": 404,
        "outside-root": 403,
        "denied-path": 403,
        "too-large": 413,
    }.get(code, 400)
    return JSONResponse(
        {"ok": False, "code": code, "message": message},
        status_code=status,
        headers={"X-Ginno-Code": code},
    )



# ---- preview file server (path-shaped, for the HTML sandbox iframe) --------

#: ext -> Content-Type for the preview stream. Same whitelist posture as
#: ``_RAW_MIME``, but for WEB DOCUMENTS: the preview iframe loads html/htm from
#: here, and its RELATIVE sub-resources (``./style.css``, ``js/app.js``) resolve
#: against this endpoint's path — query strings do not participate in relative
#: resolution, which is why this route is path-shaped when ``raw`` is not.
#: Anything unlisted is octet-stream + attachment (its subresource load fails —
#: honest, since the browser could not render it anyway).
_PREVIEW_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".xhtml": "application/xhtml+xml",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".xml": "application/xml",
    ".csv": "text/csv",
    ".vtt": "text/vtt",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}

#: A Host header is reflected into the CSP source list, so it must be exactly a
#: hostname[:port] (IPv6 in brackets allowed). Anything else — and a missing
#: header — degrades to `default-src 'none'`: the page renders inert rather
#: than trusting unparseable input.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")

#: The sandbox, stated twice. The iframe element carries ``sandbox=
#: "allow-scripts"`` (client), AND every response from this endpoint carries a
#: CSP ``sandbox`` directive (here). The CSP half is the one that matters:
#: CORS below is ``allow_origins=["*"]``, so WITHOUT it a previewed page could
#: fetch this API from its sandboxed (opaque) origin and read the responses —
#: file listings, session data. With it, the document is origin-opaque even on
#: a direct top-level navigation, and ``connect-src 'none'`` takes away fetch/
#: XHR/WebSocket entirely; ``form-action 'none'`` takes away beacon-by-form.
#: Subresource tags (img/script/link) still load from this origin, which is the
#: whole point of the preview.
_CSP_TMPL = (
    "sandbox allow-scripts; "
    "default-src {origin} data: blob: 'unsafe-inline' 'unsafe-eval'; "
    "connect-src 'none'; form-action 'none'"
)


@router.get("/api/code/file/{project_slug}/{session_id}/{root_id}/{rel_path:path}")
async def code_file(
    project_slug: str,
    session_id: str,
    root_id: str,
    rel_path: str,
    request: Request,
) -> Response:
    """A workspace file's bytes AT A PATH-SHAPED URL (HTML preview support).

    Serves the same fence as ``read``/``raw`` (design §4.6) with the same
    32 MB ceiling. The doc comment on ``_CSP_TMPL`` explains the sandboxing;
    the shape of the route (segments, not a query string) is what lets a
    previewed page's relative sub-resources land back on this endpoint.

    Failures reuse ``_raw_error``: non-2xx + ``X-Ginno-Code``, because the
    HTML preview fetches the URL once to check reachability before pointing
    the iframe at it.
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _raw_error("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        target, _root_info = resolve_code_target(ctx, root_id, rel_path, write=False)
    except CodeAccessError as e:
        return _raw_error(e.code, e.message)
    if not target.is_file():
        return _raw_error("not-text", f"Not a file: {rel_path!r}")
    try:
        size = target.stat().st_size
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")
    if size > RAW_MAX_BYTES:
        return _raw_error("too-large", f"File too large ({size / 1048576:.1f} MB) to preview")
    try:
        data = target.read_bytes()
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")

    host = request.headers.get("host", "")
    origin = f"http://{host}" if _HOST_RE.match(host) else "'none'"
    headers = {
        # A workspace file must never talk the webview into treating it as
        # markup: octet-stream stays octet-stream (same line of defence as raw).
        "X-Content-Type-Options": "nosniff",
        # Sub-resources resolve WITHOUT the entry file's ?v= (relative
        # resolution drops the query), so a versioned cache key is impossible
        # here — revalidation is what keeps a rewritten page from mixing old
        # assets with new markup.
        "Cache-Control": "no-cache",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": _CSP_TMPL.format(origin=origin),
    }
    mime = _PREVIEW_MIME.get(target.suffix.lower())
    if mime is None:
        headers["Content-Disposition"] = "attachment"
    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )

