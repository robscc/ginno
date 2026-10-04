"""File operations for the code panel — new / rename / move / delete (S4).

Brief: ``.claude/code-panel-s4-brief.md`` §3.2 (endpoints) and §1 (hard rules);
design: ``docs/code-panel-design.md`` §4.4 (the Rust twin of these operations),
§4.6 (the fence) and D6 (delete = trash, never ``rm``).

The read/write endpoints live in ``api/code.py``, which another workstream owns;
this module is a second router that REUSES their fence instead of copying it.
Every path here goes through ``resolve_code_target(..., write=True)`` — the one
implementation of the ``ro`` tier, the ``.git`` rule, symlink re-resolution and
the permanent deny regions (design §4.6). Nothing in this file re-implements a
gate.

Three things are easy to get wrong and are handled explicitly:

* **The parent is resolved, not just the leaf.** A write target usually does not
  exist yet, so ``resolve_code_target`` cannot dereference the leaf itself; the
  parent chain must still resolve inside the root and be writable, or ``a/b``
  could escape through a symlinked ``a`` while the leaf ``b`` looks contained.
* **A root is never renamed / moved / deleted.** The empty relative path is the
  root, and it is refused up front with ``root-immutable`` rather than being
  caught later by an accidental ``exists``.
* **``delete`` never unlinks.** In the Tauri app the frontend calls the Rust
  ``code_trash`` command (the ``trash`` crate → the OS trash, restorable). This
  endpoint is the browser / dev fallback, where no trash API exists, so it MOVES
  the entry into a per-delete ``.trash-<epoch_ns>/`` directory inside the
  session workspace. That is a deliberate compromise (brief §3.2): it is not the
  OS trash — the user must be told, and the response says so — but it is
  reversible and it is never ``os.remove``. No new dependency is added for it.

Response shape follows the house ``{ok, ...}`` envelope (HTTP 200 even on
failure); failures carry an ``append-only`` ``code`` the UI maps to copy.
"""

from __future__ import annotations

import shutil
import time

from fastapi import APIRouter

from . import code as code_panel
from .code import CodeAccessError, _err, _norm_rel, _session_ctx, resolve_code_target

router = APIRouter()

#: One path component may be at most this many bytes (design §4.6: ≤255 bytes).
#: Measured in UTF-8 bytes, not characters — the filesystem limit is bytes, so a
#: 90-character Chinese name is already over it.
NAME_MAX_BYTES = 255

#: Codes added by this module (design §4.3's list is append-only):
#:   ``bad-name``         — the requested name component is unusable
#:   ``exists``           — the destination is already taken (never overwritten)
#:   ``root-immutable``   — the root itself cannot be renamed / moved / deleted
#:   ``confirm-required`` — a non-empty directory needs an explicit confirmation
#:   ``invalid-target``   — the destination is structurally impossible (a
#:                          directory moved inside itself)
#: Each is deliberately its own code rather than a recycled one: the UI maps a
#: code to copy, and e.g. reporting "moved into itself" as ``conflict`` would
#: render the file-conflict bar.


# ---- helpers ---------------------------------------------------------------


def _body(req: dict) -> dict:
    return req if isinstance(req, dict) else {}


def _split_rel(rel: str) -> tuple[str, str]:
    """``(parent_rel, name)`` for a normalized root-relative path.

    ``("", "")`` for the root itself; ``("", "a.txt")`` for a top-level entry.
    """
    rel = _norm_rel(rel)
    if not rel:
        return "", ""
    if "/" in rel:
        parent, name = rel.rsplit("/", 1)
        return parent, name
    return "", rel


def _name_error(name: str) -> str | None:
    """Why ``name`` is not a usable path component, or ``None`` when it is.

    The fence checks CONTAINMENT; this checks the NAME. Both are needed: a name
    is what the user typed, and the tree renders whatever comes back, so ``..``,
    an embedded separator or a NUL would be a problem even inside a root.
    """
    if not name:
        return "名称不能为空"
    if name in (".", ".."):
        return "名称不能是 . 或 .."
    if "/" in name or "\\" in name:
        return "名称不能包含路径分隔符"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return "名称不能包含控制字符"
    if len(name.encode("utf-8")) > NAME_MAX_BYTES:
        return "名称过长（超过 255 字节）"
    return None


def _resolve(ctx: dict, root: str, rel: str):
    """The shared fence, always in its write form (brief §1-4).

    A local alias so every call site in this module visibly walks the SAME
    resolver — never a re-implementation of the ``ro`` / ``.git`` rules.
    """
    return resolve_code_target(ctx, root, rel, write=True)


def _confirmed(body: dict, count: int) -> bool:
    """Has the caller confirmed deleting a directory that holds ``count`` items?

    Accepts an explicit ``confirm: true``, or a ``confirm_count`` echo of the
    ``count`` this endpoint already returned in its ``confirm-required`` answer —
    so a client that only saw the number can confirm without another round trip.
    An empty directory needs no confirmation (there is nothing to lose).
    """
    if body.get("confirm") is True:
        return True
    try:
        return int(body.get("confirm_count")) == count
    except (TypeError, ValueError):
        return False


def _ctx_or_error(body: dict) -> tuple[dict | None, dict | None]:
    """``(ctx, error)`` — exactly one of the two is not ``None``."""
    ctx = _session_ctx(
        str(body.get("project_slug") or "default"),
        str(body.get("session_id") or ""),
    )
    if ctx is None:
        return None, _err(
            "unknown-root", f"会话不存在或已被删除：{body.get('session_id')}"
        )
    return ctx, None


# ---- POST /api/code/mkdir --------------------------------------------------


@router.post("/api/code/mkdir")
async def code_mkdir(req: dict) -> dict:
    """Create a new directory — or an empty file with ``kind: "file"``.

    ``path`` is the FULL root-relative path of the new entry (parent + name).

    ``kind`` is an extension over the brief's four-endpoint contract: design
    §4.5's context menu offers 「新建文件」 as well as 「新建文件夹」, and the
    write endpoint cannot create a file (it answers ``absent`` for one that does
    not exist yet). One endpoint with a ``kind`` switch keeps the wire surface
    the brief fixed. It defaults to ``"dir"`` so a client that only knows about
    folders is unaffected.
    """
    body = _body(req)
    ctx, err = _ctx_or_error(body)
    if err is not None:
        return err
    root = str(body.get("root") or "session")
    path = str(body.get("path") or "")
    kind = str(body.get("kind") or "dir").strip() or "dir"
    if kind not in ("dir", "file"):
        return _err("bad-name", f"未知的 kind：{kind!r}")

    parent_rel, name = _split_rel(path)
    bad = _name_error(name)
    if bad is not None:
        return _err("bad-name", bad)

    try:
        # The RAW path first, so an absolute one is refused by the resolver's
        # own gate rather than being normalized into a relative-looking path.
        # The full path also covers a symlinked PARENT component: the leaf does
        # not exist yet, so resolving it is what dereferences the parents.
        target, _ri = _resolve(ctx, root, path)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    try:
        # Then the parent, which must exist, be a directory, and be writable
        # (this is the gate that gives a useful "parent missing" answer, and it
        # refuses an ``ro`` mount before anything is touched).
        parent, _pin = _resolve(ctx, root, parent_rel)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not parent.is_dir():
        return _err("not-directory", f"上级目录不存在：{parent_rel!r}")

    if target.exists():
        return _err("exists", f"已存在同名条目：{_norm_rel(path)!r}")

    try:
        if kind == "file":
            # "x" is O_CREAT|O_EXCL: an entry that appeared between the check
            # above and here is a race we lose loudly rather than overwrite.
            with target.open("xb"):
                pass
        else:
            target.mkdir()
    except FileExistsError:
        return _err("exists", f"已存在同名条目：{_norm_rel(path)!r}")
    except OSError as e:
        return _err("denied-path", f"无法创建：{e}")
    return {"ok": True, "path": _norm_rel(path), "entry": code_panel._entry(target)}


# ---- POST /api/code/rename and /api/code/move ------------------------------


async def _relocate(body: dict, *, what: str) -> dict:
    """Shared body of ``rename`` and ``move`` — a same-root relocation.

    The two endpoints take IDENTICAL input (``path`` + a root-relative ``to``)
    and do the same filesystem operation; they exist separately because the UI
    reaches them from different gestures (inline rename vs. drag). They are
    deliberately not "rename keeps the parent, move does not": a client that
    sends ``to`` as a bare name would then silently go to the wrong place.
    Cross-root moves are out of scope (brief §1-7) — ``to`` must resolve inside
    the same root, which the fence guarantees.
    """
    ctx, err = _ctx_or_error(body)
    if err is not None:
        return err
    root = str(body.get("root") or "session")
    src_in = str(body.get("path") or "")
    dst_in = str(body.get("to") or "")
    # Normalized copies for the checks and the echo; the resolver is always fed
    # the RAW client string, because `_norm_rel` strips a leading "/" and would
    # otherwise erase the resolver's absolute-path gate (an absolute `to` must be
    # `outside-root`, not silently reinterpreted as root-relative).
    src_rel = _norm_rel(src_in)
    dst_rel = _norm_rel(dst_in)

    # Rule: a root is never renamed or moved (both ends).
    if not src_rel:
        return _err("root-immutable", "工作区根自身不可重命名或移动")
    if not dst_rel:
        return _err("root-immutable", "不能把条目移动到根自身")

    parent_rel, name = _split_rel(dst_rel)
    bad = _name_error(name)
    if bad is not None:
        return _err("bad-name", bad)

    try:
        src, _sroot = _resolve(ctx, root, src_in)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not src.exists():
        return _err("absent", f"条目已不存在：{src_rel!r}")

    try:
        dst, _droot = _resolve(ctx, root, dst_in)
        dparent, _proot = _resolve(ctx, root, parent_rel)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not dparent.is_dir():
        return _err("not-directory", f"目标上级目录不存在：{parent_rel!r}")
    if dst.exists():
        return _err("exists", f"目标已存在：{dst_rel!r}")

    # A directory cannot be moved inside itself (``os.rename`` would raise, or
    # worse, succeed partially on a platform that allows it). Compared on the
    # resolved paths so a symlink alias does not slip through.
    if src.is_dir():
        src_real = src.resolve()
        dst_real = dst.resolve()
        if dst_real == src_real or dst_real.is_relative_to(src_real):
            return _err("invalid-target", "不能把目录移动到它自身内部")

    try:
        # ``os.rename`` on macOS/Linux either moves atomically or fails; it never
        # overwrites here because ``dst`` does not exist (checked above).
        src.rename(dst)
    except OSError as e:
        return _err("denied-path", f"无法{what}：{e}")
    return {
        "ok": True,
        "from": src_rel,
        "path": dst_rel,
        "entry": code_panel._entry(dst),
    }


@router.post("/api/code/rename")
async def code_rename(req: dict) -> dict:
    """Rename / relocate one entry within its root (see ``_relocate``)."""
    return await _relocate(_body(req), what="重命名")


@router.post("/api/code/move")
async def code_move(req: dict) -> dict:
    """Drag-and-drop move — same-root only (see ``_relocate``)."""
    return await _relocate(_body(req), what="移动")


# ---- POST /api/code/delete -------------------------------------------------


@router.post("/api/code/delete")
async def code_delete(req: dict) -> dict:
    """Move an entry to the trash — NEVER a permanent delete (design D6).

    Two behaviours worth spelling out:

    * A non-empty directory needs an explicit confirmation. The first call comes
      back as ``confirm-required`` carrying ``count`` (the number of immediate
      children, the number design §4.6 asks to report); the client confirms with
      ``confirm: true`` or ``confirm_count: <count>``. This is why the endpoint
      can answer without deleting.
    * ``check_only: true`` stops after the fence and the count, so a client that
      trashes through the OS (the Rust ``code_trash`` command) can still get the
      read-only tier enforced by the one component that knows about mounts.
    * The fallback "trash" is a ``.trash-<epoch_ns>/`` directory in the session
      workspace, not the OS trash. In the packaged app the frontend never calls
      this endpoint for a delete — it calls the Rust ``code_trash`` command,
      which uses the real trash — so this path only runs in the browser / dev
      build, where no trash API exists. The compromise is stated in the response
      ``message`` so it can be surfaced to the user (brief §3.2).
    """
    body = _body(req)
    ctx, err = _ctx_or_error(body)
    if err is not None:
        return err
    root = str(body.get("root") or "session")
    path_in = str(body.get("path") or "")
    rel = _norm_rel(path_in)

    if not rel:
        return _err("root-immutable", "工作区根自身不可删除")

    try:
        # RAW, for the same reason as `_relocate`: normalization would drop an
        # absolute path's leading "/" before the resolver could refuse it.
        target, _root_info = _resolve(ctx, root, path_in)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not target.exists() and not target.is_symlink():
        return _err("absent", f"条目已不存在：{rel!r}")

    # `check_only` runs the FENCE and reports the count WITHOUT deleting.
    #
    # The packaged app needs exactly this: its delete ends in the Rust
    # ``code_trash`` command, which cannot enforce the ``ro`` tier or the
    # ``.git`` rule — it only knows path containment, and `mount_access` is
    # MOST-SPECIFIC-MATCH, so a read-only mount nested inside a writable root
    # looks writable when judged from the root alone. A per-root `writable` flag
    # in the UI therefore cannot express it. So the client validates here first
    # (where the real fence lives) and only then asks the OS to trash the entry.
    if body.get("check_only"):
        count = 0
        if target.is_dir() and not target.is_symlink():
            count = code_panel._count_immediate(target) or 0
        return {"ok": True, "check_only": True, "count": count}

    # Rule (design §4.6): deleting a directory that still holds content needs a
    # second, informed confirmation, and the count is what informs it.
    count = 0
    if target.is_dir() and not target.is_symlink():
        count = code_panel._count_immediate(target) or 0
        if count > 0 and not _confirmed(body, count):
            return {
                "ok": False,
                "code": "confirm-required",
                "count": count,
                "message": f"This folder contains {count} items; they can be restored from Trash after deletion. Are you sure?",
            }

    workspace = ctx["workspace"]
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return _err("denied-path", f"Failed to prepare the trash folder: {e}")
    trash_dir = workspace / f".trash-{time.time_ns()}"
    dest = trash_dir / target.name
    try:
        trash_dir.mkdir()
        # ``shutil.move`` falls back to copy+delete across filesystems (a mount
        # is usually on another volume than ~/.ginno), so this works for both the
        # workspace and a mounted root while still never unlinking outright.
        shutil.move(str(target), str(dest))
    except OSError as e:
        return _err("denied-path", f"Failed to move to the trash folder: {e}")

    return {
        "ok": True,
        "trashed": True,
        "count": count,
        "trash_path": str(dest),
        "message": f"Moved to the session trash folder ({dest.name}), not the system Trash; restore manually if needed",
    }