"""Code panel — S4 search: name (⌘P) and content (⇧⌘F) over one root.

Split from the former single-module ``api/code.py``;面板总述见
``api/code/__init__.py``。预算镜像 ``tools/builtin`` 的 glob/grep 上限,
扫描永不静默截断(truncated/scanned 必须诚实)。
"""

from __future__ import annotations

import os
import re
import time
from collections import deque
from pathlib import Path

from ...tools.builtin import (
    GLOB_MAX_DIRS,
    GLOB_MAX_HITS,
    GLOB_TIME_BUDGET_S,
    _path_denied,
)

from ._common import (
    EDITABLE_MAX_BYTES,
    HEAVY_DIRS,
    CodeAccessError,
    _decode,
    _err,
    _session_ctx,
    _unknown_session,
    resolve_code_target,
    router,
)


# ---- search (S4) -----------------------------------------------------------

#: Name search stops after visiting this many directory entries (files AND
#: dirs, including ones pruned as heavy/denied — the readdir slot was spent
#: either way). Same order as ``builtin.GLOB_MAX_DIRS`` so both walks degrade
#: alike (design §4.1: 扫 20000 项 / 返 50).
SEARCH_NAME_MAX_ENTRIES = 20000
#: Name search also stops on wall clock, mirroring ``grep_files``.
SEARCH_NAME_TIME_BUDGET_S = GLOB_TIME_BUDGET_S
#: Content search collects at most this many hits before stopping — the same
#: budget ``grep_files`` uses. ``limit`` only trims what is RETURNED: the scan
#: keeps collecting up to this cap so ``truncated`` can be honest when more
#: matches exist than the caller asked for (brief §1-2).
SEARCH_CONTENT_MAX_HITS = GLOB_MAX_HITS
#: Content search also bounds the walk by directory count and wall clock, the
#: two budgets ``grep_files`` already uses.
SEARCH_CONTENT_MAX_DIRS = GLOB_MAX_DIRS
SEARCH_CONTENT_TIME_BUDGET_S = GLOB_TIME_BUDGET_S
#: A file larger than this is not read: content search is for source trees, not
#: for multi-hundred-MB logs. Same ceiling the editor uses for editing.
SEARCH_MAX_FILE_BYTES = EDITABLE_MAX_BYTES
#: ``q`` ceiling in bytes — a megabyte-long query is a client bug, not a search.
SEARCH_MAX_QUERY_BYTES = 1000
#: Returned-hit page size (design §4.1: name search returns 50).
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 200


def _subsequence(needle: str, hay: str) -> bool:
    """True when every char of ``needle`` occurs IN ORDER inside ``hay``.

    ``ch in it`` advances the character iterator, which is the whole algorithm:
    it fails as soon as a needed character is no longer ahead.
    """
    it = iter(hay)
    return all(ch in it for ch in needle)


def _name_rank(rel: str, q: str) -> int | None:
    """Match quality of a root-relative file path against a lowered query.

    ``0`` = query is a case-insensitive SUBSTRING of the basename (the common
    "I remember the file name" case), ``1`` = substring of the full path (the
    query spans a directory), ``2`` = SUBSEQUENCE of the path (typo-tolerant
    fuzzy), ``None`` = no match.

    Of the two matchers the brief allows (subsequence vs case-insensitive
    substring), this endpoint uses **subsequence**, because it backs ⌘P quick
    open: users type fragments and expect near misses to surface (``srch`` →
    ``search.py``), where a plain substring filter returns nothing for one
    dropped letter. Rank keeps the common case (a basename hit) on top so fuzzy
    noise never outranks an obvious answer.
    """
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    if q in base:
        return 0
    if q in low:
        return 1
    return 2 if _subsequence(q, low) else None


def _search_name(
    root: Path, query: str, limit: int, ctx: dict
) -> tuple[list[str], int, bool]:
    """BFS ``root`` for files whose path matches ``query`` (design §4.2-4).

    Returns ``(paths, scanned, truncated)``: ``scanned`` counts directory
    entries VISITED (not matches), and ``truncated`` means the walk stopped
    early on the entry/time budget — more files may exist.

    The walk never descends into a ``HEAVY_DIRS`` directory and never follows or
    matches a symlink: a quick open must not spend its budget inside
    ``node_modules``, nor leave the root through a link. Directories are not
    returned — the wire ``Hit`` is just a path, and ⌘P opens a file.

    BFS (not ``os.walk``'s depth-first order) so that when the budget does run
    out, the entries nearest the root are the ones already seen — the useful
    half of the tree for a quick open.
    """
    q = query.lower()
    found: list[tuple[int, int, int, str, str]] = []
    scanned = 0
    truncated = False
    t0 = time.monotonic()
    base_dir = ctx["workspace"]
    mount_roots = ctx["mount_roots"]
    queue: deque[Path] = deque([root])
    while queue:
        if time.monotonic() - t0 > SEARCH_NAME_TIME_BUDGET_S:
            truncated = True
            break
        d = queue.popleft()
        try:
            children = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue  # unreadable dir: skip it, never fail the whole search
        for child in children:
            scanned += 1
            if scanned > SEARCH_NAME_MAX_ENTRIES:
                truncated = True
                break
            if child.is_symlink():
                continue
            if _path_denied(child, base_dir, mount_roots):
                continue
            try:
                if child.is_dir():
                    if child.name not in HEAVY_DIRS:
                        queue.append(child)
                    continue
                if not child.is_file():
                    continue
            except OSError:
                continue
            rel = child.relative_to(root).as_posix()
            rank = _name_rank(rel, q)
            if rank is not None:
                # Sort key: quality, then shallowest, then shortest, then name.
                found.append((rank, rel.count("/"), len(rel), rel.lower(), rel))
        if truncated:
            break
    found.sort()
    return [f[4] for f in found[:limit]], scanned, truncated


def _search_content(
    root: Path, rx: "re.Pattern[str]", limit: int, ctx: dict
) -> tuple[list[dict], int, bool]:
    """Walk ``root`` and match ``rx`` line by line (design §4.2-4).

    Mirrors ``tools.builtin.grep_files`` — ``os.walk`` + a per-file read + a
    per-line ``rx.search`` — with the ONE addition the design demands: the
    ``HEAVY_DIRS`` skip set is pruned on top of ``_path_denied``, because
    ``_path_denied`` does NOT exclude ``node_modules`` (it only guards
    ``~/.ssh`` / the keychain / ``~/.ginno``). Without that prune the 20000-dir
    budget is spent inside ``node_modules`` and the search returns noise.

    Files are decoded with this module's probe order (utf-8 → gbk → latin-1),
    so a GBK source still matches where ``grep_files``' utf-8-only read would
    silently miss it. Binaries (a NUL byte in the body) and files over
    ``SEARCH_MAX_FILE_BYTES`` are skipped; symlinks are never read (they can
    point outside the root, which this fence is scoped to).

    ``rx`` is case-SENSITIVE, exactly like ``grep_files``: ``q`` is a regex and
    the caller controls case with ``(?i)``. Returns ``(hits, scanned,
    truncated)``; ``scanned`` counts FILES read.
    """
    hits: list[dict] = []
    scanned = 0
    t0 = time.monotonic()
    dirs_seen = 0
    budget_out = False  # time/dir budget hit — the walk really stopped early
    capped = False  # hit budget hit
    base_dir = ctx["workspace"]
    mount_roots = ctx["mount_roots"]
    for r, dirs, files in os.walk(root):
        dirs_seen += 1
        if (
            dirs_seen > SEARCH_CONTENT_MAX_DIRS
            or time.monotonic() - t0 > SEARCH_CONTENT_TIME_BUDGET_S
        ):
            budget_out = True
            break
        # Prune BEFORE descending: heavy dirs first (the §1-1 fix), then the
        # permanent deny regions grep_files already prunes.
        dirs[:] = [
            d
            for d in dirs
            if d not in HEAVY_DIRS
            and not _path_denied(Path(r) / d, base_dir, mount_roots)
        ]
        for name in sorted(files):
            fp = Path(r) / name
            if fp.is_symlink() or _path_denied(fp, base_dir, mount_roots):
                continue
            scanned += 1
            try:
                if fp.stat().st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                data = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue
            text, _enc = _decode(data)
            rel = fp.relative_to(root).as_posix()
            for i, line in enumerate(text.splitlines(), 1):
                if not rx.search(line):
                    continue
                hits.append({"path": rel, "line": i, "text": line})
                if len(hits) >= SEARCH_CONTENT_MAX_HITS:
                    capped = True
                    break
            if capped:
                break
        if capped:
            break
    # Honest truncation: the scan stopped early (budget) OR more matches were
    # found than are being returned. Either way the caller must be told.
    return hits[:limit], scanned, budget_out or capped or len(hits) > limit


@router.get("/api/code/search")
async def code_search(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    q: str = "",
    mode: str = "name",
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict:
    """Search one root by name (⌘P) or content (⇧⌘F) (brief §3.1, design §4.6).

    Two rules this endpoint must not break:

    * **Never truncate silently.** ``truncated`` is set whenever the walk/scan
      stopped before covering the tree, and ``scanned`` says how much it did
      cover, so the panel can say "已扫描 N 个文件后停止，结果可能不完整" instead
      of presenting a partial result as complete. An exhausted budget is a
      *successful* response with ``truncated: true`` (the partial results are
      the useful half) — ``search-timeout`` is reserved for a hard failure and
      is not used here.
    * **No server-side cache.** The design floats one (§4.1), but the client
      calls this on demand and freshness matters right after a save or an agent
      edit, so a cache would add invalidation bugs for a call the user triggers
      explicitly (and the two budgets already bound the cost).
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    try:
        # Same fence as `list`/`read`: an unreachable root gets the same error
        # code here, and the walk stays inside the resolved root.
        target, _root_info = resolve_code_target(ctx, root, "", write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    m = (mode or "name").strip().lower()
    if m not in ("name", "content"):
        return _err("invalid-mode", f"Unknown search mode: {mode!r}")
    query = q or ""
    if not query.strip():
        return _err("invalid-query", "Search text must not be empty")
    if len(query.encode("utf-8")) > SEARCH_MAX_QUERY_BYTES:
        return _err("invalid-query", f"Search text too long (max {SEARCH_MAX_QUERY_BYTES} bytes)")

    # `limit` is a page size, not a budget: clamp rather than reject (an
    # out-of-range limit is a client bug, and 200 is a perfectly good answer).
    page = max(1, min(int(limit or SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MAX))

    t0 = time.monotonic()
    if m == "name":
        paths, scanned, truncated = _search_name(target, query, page, ctx)
        hits: list[dict] = [{"path": p} for p in paths]
    else:
        try:
            rx = re.compile(query)
        except re.error as e:
            # A bad pattern is the caller's mistake, not a server fault: fail
            # with a code and copy, never a 500 (brief §3.1).
            return _err("invalid-regex", f"Invalid regular expression: {e}")
        hits, scanned, truncated = _search_content(target, rx, page, ctx)
    return {
        "ok": True,
        "mode": m,
        "hits": hits,
        "scanned": scanned,
        "truncated": truncated,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }

