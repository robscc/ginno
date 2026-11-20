"""Code panel — S2 write endpoint (atomic save with version conflict refusal).

Split from the former single-module ``api/code.py``;面板总述见
``api/code/__init__.py``。
"""

from __future__ import annotations

import os
import tempfile

from fastapi import Response
from fastapi.responses import JSONResponse

from ._common import (
    EDITABLE_MAX_BYTES,
    CodeAccessError,
    _err,
    _session_ctx,
    _unknown_session,
    resolve_code_target,
    router,
)


# ---- write (S2) ------------------------------------------------------------


@router.put("/api/code/write")
async def code_write(req: dict) -> Response:
    """Save an open file (design §4.7-S2, brief §3.1).

    The request carries the ``base_version`` the buffer was read at. A mismatch
    means the file changed on disk since (the agent editing the same file is the
    expected case, not the exotic one), and the write is REFUSED with 409 plus
    the version currently on disk — never silently applied. That "announce, do
    not overwrite" rule is the whole point of the version token; it is also the
    only unrecoverable thing this panel could do (brief §1-2/§1-5).

    The old bytes are replaced atomically: a same-directory temp file, chmod-ed
    back to the original ``st_mode``, then ``os.replace``. Truncate-and-write
    would leave a half file behind on a crash, and a bare ``mkstemp`` rename
    would strip ``+x`` off a script and turn every ordinary 0644 file into 0600
    (brief §1-4).
    """
    body = req if isinstance(req, dict) else {}
    project_slug = str(body.get("project_slug") or "default")
    session_id = str(body.get("session_id") or "")
    root = str(body.get("root") or "session")
    path = str(body.get("path") or "")
    content = body.get("content")
    base_version = str(body.get("base_version") or "")
    # The client echoes the encoding `read` reported; defaulting to utf-8 is the
    # S1 read default too. Writing a GBK file back as UTF-8 would be silent
    # corruption (brief §1-3), so the caller's value is used verbatim.
    encoding = str(body.get("encoding") or "utf-8").strip() or "utf-8"

    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    try:
        # The `ro` tier and the `.git` rule live in here — same fence as `read`,
        # deliberately not a second copy of it (design §4.6).
        target, _root_info = resolve_code_target(ctx, root, path, write=True)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    if not isinstance(content, str):
        return _err("not-text", "content must be a string")

    # Distinguish "deleted out from under us" (absent — the panel should offer
    # to recreate, not to retry) from "that path is not a file" (not-text).
    if not target.exists():
        return _err("absent", f"File no longer exists: {path!r}")
    if not target.is_file():
        return _err("not-text", f"Not a file: {path!r}")
    try:
        st = target.stat()
    except OSError as e:
        return _err("not-text", f"Failed to stat file: {e}")

    # Version format must stay identical to `read`'s, or every save would look
    # like a conflict.
    disk_version = f"{st.st_size}:{st.st_mtime_ns}"
    if base_version != disk_version:
        return JSONResponse(
            {
                "ok": False,
                "code": "conflict",
                "version": disk_version,
                "message": "File was modified on disk; not saved",
            },
            status_code=409,
        )

    try:
        data = content.encode(encoding)
    except UnicodeEncodeError:
        # e.g. an emoji into GBK: the content simply has no representation in
        # the file's encoding. Say so instead of mangling it or swapping the
        # encoding behind the user's back.
        return _err("not-text", f"Content cannot be saved as {encoding} (contains characters the encoding can't represent)")
    except LookupError:
        return _err("not-text", f"Unsupported encoding: {encoding}")
    if len(data) > EDITABLE_MAX_BYTES:
        return _err("too-large", f"Content too large ({len(data) / 1048576:.1f} MB) to save")

    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Before the rename, so the file never appears with mkstemp's 0600.
        os.chmod(tmp, st.st_mode)
        os.replace(tmp, target)
        tmp = None
    except OSError as e:
        return _err("not-text", f"Failed to write file: {e}")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Re-stat rather than deriving the version from `data`: the returned version
    # must describe what is on disk NOW, and only stat knows that.
    try:
        st_after = target.stat()
    except OSError as e:
        return _err("not-text", f"Failed to stat file after write: {e}")
    return {"ok": True, "version": f"{st_after.st_size}:{st_after.st_mtime_ns}"}

