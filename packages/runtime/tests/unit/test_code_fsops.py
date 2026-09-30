"""File operations from the code panel: ``/api/code/{mkdir,rename,move,delete}``.

Brief: ``.claude/code-panel-s4-brief.md`` §3.2; design §4.6 (fence) and D6.

Every test here pins a rule from the brief §1 rather than a code path:

* **The fence is the shared one.** ``ro`` mounts and ``.git`` interiors are
  refused because these endpoints call ``resolve_code_target(write=True)``, not
  because they grew their own check — so the assertions are on the wire code
  (``read-only-mount`` / ``denied-path``), which is what stays true if the
  resolver is refactored.
* **A root is never renamed / moved / deleted.** The empty relative path is the
  root: it must come back ``root-immutable`` rather than, say, moving the whole
  workspace into its own trash.
* **Delete is not ``rm``.** The deleted entry must still exist, byte for byte,
  under the session's ``.trash-*`` directory — a test that only asserted "the
  original path is gone" would pass for a permanent delete too.
* **A cross-root ``to`` is refused** (`to` is root-relative, so an escape is a
  containment failure).

Sessions are warm (``_SESSIONS``), like ``test_code_write.py``; ``isolated_home``
(tests/conftest.py, autouse) points ``$GINNO_HOME`` at a fresh tmp dir and
``tmp_path_factory`` provides the mount dirs OUTSIDE it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code_fsops
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-fsops"
Q = {"project_slug": SLUG, "session_id": SID}


@pytest.fixture
def env(tmp_path_factory):
    """A warm session with an ``rw`` mount, an ``ro`` mount, and its workspace."""
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    rw = tmp_path_factory.mktemp("rw-mount")
    ro = tmp_path_factory.mktemp("ro-mount")
    _SESSIONS[SID] = {
        "project_slug": SLUG,
        "context_dirs": [
            {"id": "f_rw", "path": str(rw), "name": "rw", "access": "rw"},
            {"id": "f_ro", "path": str(ro), "name": "ro", "access": "ro"},
        ],
        "primary_path": str(rw),
    }
    app = FastAPI()
    app.include_router(code_fsops.router)
    try:
        yield TestClient(app), ws, rw, ro
    finally:
        _SESSIONS.pop(SID, None)


def _post(c: TestClient, route: str, **kw) -> dict:
    return c.post(f"/api/code/{route}", json={**Q, **kw}).json()


# --------------------------------------------------------------------------- #
# mkdir / new file
# --------------------------------------------------------------------------- #
def test_mkdir_creates_a_directory_and_reports_its_entry(env):
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="notes")

    assert r["ok"] is True
    assert r["path"] == "notes"
    assert r["entry"]["name"] == "notes"
    assert r["entry"]["type"] == "dir"
    assert (ws / "notes").is_dir()


def test_mkdir_can_create_a_nested_directory(env):
    """A nested path works as long as the PARENT already exists."""
    c, ws, _rw, _ro = env
    (ws / "src").mkdir()

    r = _post(c, "mkdir", root="session", path="src/inner")

    assert r["ok"] is True
    assert (ws / "src" / "inner").is_dir()


def test_mkdir_under_a_missing_parent_is_not_directory(env):
    """``mkdir`` is one level: it does not invent the parent silently (a typo in
    the parent should be reported, not hidden by a recursive create)."""
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="ghost/child")

    assert r["ok"] is False
    assert r["code"] == "not-directory"
    assert not (ws / "ghost").exists()


def test_mkdir_kind_file_creates_an_empty_file(env):
    """「新建文件」 (design §4.5) — the write endpoint cannot create a file that
    does not exist yet, so ``mkdir`` takes ``kind: "file"``."""
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="a.txt", kind="file")

    assert r["ok"] is True
    assert r["entry"]["type"] == "file"
    assert r["entry"]["size"] == 0
    assert (ws / "a.txt").read_bytes() == b""


def test_mkdir_kind_defaults_to_dir(env):
    """A client that only knows folders is unaffected by the extension."""
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="plain")

    assert r["ok"] is True
    assert (ws / "plain").is_dir()


def test_an_unknown_kind_is_refused(env):
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="x", kind="socket")

    assert r["ok"] is False
    assert r["code"] == "bad-name"
    assert not (ws / "x").exists()


def test_mkdir_on_an_existing_name_is_exists(env):
    """An existing entry is reported, never overwritten (D6's spirit: the tree
    has no destructive shortcut)."""
    c, ws, _rw, _ro = env
    (ws / "taken").mkdir()
    (ws / "taken" / "keep.txt").write_text("mine\n", encoding="utf-8")

    r = _post(c, "mkdir", root="session", path="taken")

    assert r["ok"] is False
    assert r["code"] == "exists"
    assert (ws / "taken" / "keep.txt").read_text(encoding="utf-8") == "mine\n"


def test_mkdir_kind_file_on_an_existing_file_is_exists(env):
    c, ws, _rw, _ro = env
    (ws / "a.txt").write_text("hello\n", encoding="utf-8")

    r = _post(c, "mkdir", root="session", path="a.txt", kind="file")

    assert r["ok"] is False
    assert r["code"] == "exists"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "hello\n"


# --------------------------------------------------------------------------- #
# Name validation (design §4.6: no "/", not "."/"..", no control chars, ≤255 B)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "sub/..",  # ".." as the name component
        "sub/.",  # "." as the name component
        "a\x00b",  # NUL inside the name
        "a\nb",  # newline (a control char)
        "a" * 256,  # 256 bytes > 255
    ],
)
def test_an_invalid_name_is_refused(env, path):
    c, ws, _rw, _ro = env
    (ws / "sub").mkdir()

    r = _post(c, "mkdir", root="session", path=path)

    assert r["ok"] is False
    assert r["code"] == "bad-name", path


def test_an_empty_name_is_refused(env):
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="")

    assert r["ok"] is False
    assert r["code"] == "bad-name"


def test_a_name_at_the_255_byte_limit_is_accepted(env):
    """The boundary itself is legal — the check is "over 255", not ">= 255"."""
    c, ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="a" * 255)

    assert r["ok"] is True
    assert len("a" * 255) == 255


def test_a_multibyte_name_is_measured_in_bytes(env):
    """85 Chinese characters are 255 bytes — the limit is bytes, not chars, so
    86 of them are already over it even though it is only 86 characters."""
    c, ws, _rw, _ro = env

    assert _post(c, "mkdir", root="session", path="中" * 85)["ok"] is True
    r = _post(c, "mkdir", root="session", path="中" * 86)
    assert r["ok"] is False
    assert r["code"] == "bad-name"


def test_the_separator_check_is_real_even_though_the_path_form_hides_it():
    """``/`` can never reach ``_name_error`` through the wire (the path is split
    on it), so the guard is asserted directly — it is the backstop if a caller
    ever passes a name instead of a path."""
    assert code_fsops._name_error("a/b") is not None
    assert code_fsops._name_error("a\\b") is not None
    assert code_fsops._name_error("ok.txt") is None


# --------------------------------------------------------------------------- #
# The fence: ro mount / .git interior / traversal
# --------------------------------------------------------------------------- #
def test_mkdir_in_an_ro_mount_is_refused(env):
    """``ro`` is a hard constraint on every write, including a bare mkdir."""
    c, _ws, _rw, ro = env

    r = _post(c, "mkdir", root="f_ro", path="nope")

    assert r["ok"] is False
    assert r["code"] == "read-only-mount"
    assert not (ro / "nope").exists()


def test_mkdir_inside_git_is_refused(env):
    """Nothing under ``.git`` is creatable, even on an ``rw`` mount."""
    c, _ws, rw, _ro = env
    (rw / ".git").mkdir()

    r = _post(c, "mkdir", root="f_rw", path=".git/hooks")

    assert r["ok"] is False
    assert r["code"] == "denied-path"
    assert not (rw / ".git" / "hooks").exists()


def test_mkdir_traversing_out_of_the_root_is_refused(env):
    """The parent half is a path too: ``../../../etc/x`` must not create
    anything above the root."""
    c, _ws, _rw, _ro = env

    r = _post(c, "mkdir", root="session", path="../../../ginno-escape")

    assert r["ok"] is False
    assert r["code"] == "outside-root"


def test_mkdir_with_an_absolute_path_is_outside_root(env, tmp_path_factory):
    """An absolute path belongs to no root (design §4.3), and the module must not
    normalize it into a relative-looking one before the resolver sees it — the
    error code is what proves the raw value reached the gate."""
    c, _ws, _rw, _ro = env
    victim = tmp_path_factory.mktemp("abs-mkdir") / "x"

    r = _post(c, "mkdir", root="session", path=str(victim))

    assert r["ok"] is False
    assert r["code"] == "outside-root"
    assert not victim.exists()


def test_mkdir_through_a_symlinked_parent_is_refused(env, tmp_path_factory):
    """The parent is resolved through the fence, so a symlinked_dir escapes in
    the PARENT component — the leaf alone would still look contained."""
    c, ws, _rw, _ro = env
    outside = tmp_path_factory.mktemp("outside")
    (ws / "linkdir").symlink_to(outside, target_is_directory=True)

    r = _post(c, "mkdir", root="session", path="linkdir/child")

    assert r["ok"] is False
    assert r["code"] == "outside-root"
    assert not (outside / "child").exists()


def test_mkdir_on_an_unknown_root_and_session(env):
    c, _ws, _rw, _ro = env

    assert _post(c, "mkdir", root="f_ghost", path="x")["code"] == "unknown-root"
    ghost = c.post(
        "/api/code/mkdir",
        json={**Q, "session_id": "ghost", "root": "session", "path": "x"},
    ).json()
    assert ghost["code"] == "unknown-root"


# --------------------------------------------------------------------------- #
# rename / move
# --------------------------------------------------------------------------- #
def test_rename_moves_a_file_within_its_parent(env):
    c, ws, _rw, _ro = env
    (ws / "old.txt").write_text("body\n", encoding="utf-8")

    r = _post(c, "rename", root="session", path="old.txt", to="new.txt")

    assert r["ok"] is True
    assert r["from"] == "old.txt"
    assert r["path"] == "new.txt"
    assert r["entry"]["name"] == "new.txt"
    assert not (ws / "old.txt").exists()
    assert (ws / "new.txt").read_text(encoding="utf-8") == "body\n"


def test_move_relocates_a_directory_into_another_directory(env):
    """Same-root drag: the directory and its contents arrive intact."""
    c, ws, _rw, _ro = env
    (ws / "pkg").mkdir()
    (ws / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "dest").mkdir()

    r = _post(c, "move", root="session", path="pkg", to="dest/pkg")

    assert r["ok"] is True
    assert r["entry"]["type"] == "dir"
    assert not (ws / "pkg").exists()
    assert (ws / "dest" / "pkg" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"


def test_rename_onto_an_existing_entry_is_exists(env):
    """No silent overwrite — the destination's bytes must survive."""
    c, ws, _rw, _ro = env
    (ws / "a.txt").write_text("A\n", encoding="utf-8")
    (ws / "b.txt").write_text("B\n", encoding="utf-8")

    r = _post(c, "rename", root="session", path="a.txt", to="b.txt")

    assert r["ok"] is False
    assert r["code"] == "exists"
    assert (ws / "b.txt").read_text(encoding="utf-8") == "B\n"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "A\n"


def test_move_into_a_missing_parent_is_not_directory(env):
    c, ws, _rw, _ro = env
    (ws / "a.txt").write_text("A\n", encoding="utf-8")

    r = _post(c, "move", root="session", path="a.txt", to="nope/a.txt")

    assert r["ok"] is False
    assert r["code"] == "not-directory"
    assert (ws / "a.txt").exists()


def test_move_of_a_vanished_source_is_absent(env):
    c, ws, _rw, _ro = env

    r = _post(c, "move", root="session", path="gone.txt", to="x.txt")

    assert r["ok"] is False
    assert r["code"] == "absent"


def test_move_of_a_directory_into_itself_is_refused(env):
    """``dest/into/pkg`` must not move ``pkg`` under itself — that would either
    raise inside ``os.rename`` or lose the tree."""
    c, ws, _rw, _ro = env
    (ws / "pkg").mkdir()
    (ws / "pkg" / "inside").mkdir()

    r = _post(c, "move", root="session", path="pkg", to="pkg/inside/pkg")

    assert r["ok"] is False
    assert r["code"] == "invalid-target"
    assert (ws / "pkg" / "inside").is_dir()


def test_move_with_an_invalid_destination_name_is_refused(env):
    c, ws, _rw, _ro = env
    (ws / "a.txt").write_text("A\n", encoding="utf-8")
    (ws / "sub").mkdir()

    r = _post(c, "move", root="session", path="a.txt", to="sub/..")

    assert r["ok"] is False
    assert r["code"] == "bad-name"
    assert (ws / "a.txt").exists()


def test_a_cross_root_to_is_refused(env, tmp_path_factory):
    """``to`` is ANOTHER ROOT-RELATIVE path: it cannot cross into a second mount
    (the traversal is a containment failure, brief §1-7)."""
    c, ws, rw, _ro = env
    (ws / "a.txt").write_text("A\n", encoding="utf-8")

    # Enough `..` to clear the workspace and land elsewhere; and an absolute
    # path, which has no root to belong to at all.
    escape = _post(c, "move", root="session", path="a.txt", to="../../../../etc/ginno-x")
    assert escape["ok"] is False
    assert escape["code"] == "outside-root"

    absolute = _post(c, "move", root="session", path="a.txt", to=str(rw / "a.txt"))
    assert absolute["ok"] is False
    assert absolute["code"] == "outside-root"
    assert (ws / "a.txt").exists()
    assert not (rw / "a.txt").exists()


def test_rename_in_an_ro_mount_is_refused(env):
    c, _ws, _rw, ro = env
    (ro / "no.txt").write_text("readable\n", encoding="utf-8")

    r = _post(c, "rename", root="f_ro", path="no.txt", to="yes.txt")

    assert r["ok"] is False
    assert r["code"] == "read-only-mount"
    assert (ro / "no.txt").exists()
    assert not (ro / "yes.txt").exists()


def test_move_out_of_git_is_refused(env):
    c, _ws, rw, _ro = env
    (rw / ".git").mkdir()
    (rw / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (rw / "out.txt").write_text("x\n", encoding="utf-8")

    # Source inside .git → denied.
    src = _post(c, "move", root="f_rw", path=".git/config", to="config")
    assert src["ok"] is False and src["code"] == "denied-path"
    # Destination inside .git → denied too (the same rule, the other end).
    dst = _post(c, "move", root="f_rw", path="out.txt", to=".git/out.txt")
    assert dst["ok"] is False and dst["code"] == "denied-path"
    assert (rw / ".git" / "config").exists()
    assert (rw / "out.txt").exists()


# --------------------------------------------------------------------------- #
# A root is never renamed / moved / deleted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route", ["rename", "move", "delete"])
def test_the_root_itself_can_never_be_renamed_moved_or_deleted(env, route):
    """The empty relative path IS the root. Deleting it would move the whole
    workspace into its own trash; renaming it has no meaning at all."""
    c, ws, _rw, _ro = env
    (ws / "keep.txt").write_text("keep\n", encoding="utf-8")

    body = {"root": "session", "path": ""}
    if route in ("rename", "move"):
        body["to"] = "elsewhere"

    r = _post(c, route, **body)

    assert r["ok"] is False
    assert r["code"] == "root-immutable"
    assert ws.is_dir()
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_moving_an_entry_onto_the_root_is_refused(env):
    """The destination end of the same rule: ``to=""`` would be a move onto the
    root directory itself."""
    c, ws, _rw, _ro = env
    (ws / "a.txt").write_text("A\n", encoding="utf-8")

    r = _post(c, "move", root="session", path="a.txt", to="")

    assert r["ok"] is False
    assert r["code"] == "root-immutable"
    assert (ws / "a.txt").exists()


# --------------------------------------------------------------------------- #
# delete = trash, never rm (design D6)
# --------------------------------------------------------------------------- #
def test_delete_moves_the_file_to_the_trash_and_it_is_still_recoverable(env):
    """Both halves of D6 in one test: the original path is gone AND the bytes
    still exist under the session's ``.trash-*`` directory.

    Asserting only the first half would pass for ``os.remove`` — which is the
    one thing this endpoint must never do."""
    c, ws, _rw, _ro = env
    (ws / "doomed.txt").write_text("import thing\n", encoding="utf-8")

    r = _post(c, "delete", root="session", path="doomed.txt")

    assert r["ok"] is True
    assert r["trashed"] is True
    assert not (ws / "doomed.txt").exists()

    trashed = list(ws.glob(".trash-*/*"))
    assert [p.name for p in trashed] == ["doomed.txt"]
    assert trashed[0].read_text(encoding="utf-8") == "import thing\n"
    # The response points at the copy, so the UI can tell the user where it went.
    assert r["trash_path"] == str(trashed[0])
    assert "废纸篓" in r["message"] or "回收" in r["message"]


def test_delete_never_leaves_the_entry_in_place(env):
    """A directory delete must take the whole tree with it (not just fail)."""
    c, ws, _rw, _ro = env
    (ws / "pkg").mkdir()
    (ws / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    r = _post(c, "delete", root="session", path="pkg", confirm=True)

    assert r["ok"] is True
    assert not (ws / "pkg").exists()
    recovered = list(ws.glob(".trash-*/pkg/mod.py"))
    assert len(recovered) == 1
    assert recovered[0].read_text(encoding="utf-8") == "x = 1\n"


def test_deleting_a_directory_with_contents_needs_confirmation(env):
    """Rule (design §4.6): report the entry count, then require a second call.

    The first response must NOT have deleted anything — that is the whole point
    of the gate."""
    c, ws, _rw, _ro = env
    (ws / "pkg").mkdir()
    (ws / "pkg" / "a.txt").write_text("a\n", encoding="utf-8")
    (ws / "pkg" / "b.txt").write_text("b\n", encoding="utf-8")

    r = _post(c, "delete", root="session", path="pkg")

    assert r["ok"] is False
    assert r["code"] == "confirm-required"
    assert r["count"] == 2
    assert "2" in r["message"]
    assert (ws / "pkg" / "a.txt").exists()  # nothing happened yet

    # Either an explicit confirm, or the count echoed back, is accepted.
    assert _post(c, "delete", root="session", path="pkg", confirm_count=2)["ok"] is True
    assert not (ws / "pkg").exists()


def test_an_empty_directory_deletes_without_confirmation(env):
    """Nothing to lose → no interruption."""
    c, ws, _rw, _ro = env
    (ws / "empty").mkdir()

    r = _post(c, "delete", root="session", path="empty")

    assert r["ok"] is True
    assert not (ws / "empty").exists()


def test_a_wrong_confirm_count_does_not_delete(env):
    """The count is the confirmation's content, not a formality: echoing a stale
    number (the directory grew) must not pass."""
    c, ws, _rw, _ro = env
    (ws / "pkg").mkdir()
    (ws / "pkg" / "a.txt").write_text("a\n", encoding="utf-8")

    r = _post(c, "delete", root="session", path="pkg", confirm_count=99)

    assert r["ok"] is False
    assert r["code"] == "confirm-required"
    assert (ws / "pkg" / "a.txt").exists()


def test_delete_in_an_ro_mount_is_refused(env):
    c, _ws, _rw, ro = env
    (ro / "no.txt").write_text("readable\n", encoding="utf-8")

    r = _post(c, "delete", root="f_ro", path="no.txt")

    assert r["ok"] is False
    assert r["code"] == "read-only-mount"
    assert (ro / "no.txt").read_text(encoding="utf-8") == "readable\n"


def test_delete_inside_git_is_refused(env):
    c, _ws, rw, _ro = env
    (rw / ".git").mkdir()
    (rw / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    r = _post(c, "delete", root="f_rw", path=".git/config")

    assert r["ok"] is False
    assert r["code"] == "denied-path"
    assert (rw / ".git" / "config").exists()


def test_delete_of_a_vanished_entry_is_absent(env):
    c, ws, _rw, _ro = env

    r = _post(c, "delete", root="session", path="ghost.txt")

    assert r["ok"] is False
    assert r["code"] == "absent"


def test_delete_traversing_out_of_the_root_is_refused(env):
    c, _ws, _rw, _ro = env

    r = _post(c, "delete", root="session", path="../../../../etc/passwd")

    assert r["ok"] is False
    assert r["code"] == "outside-root"


def test_each_delete_gets_its_own_trash_directory(env):
    """Two entries with the same name deleted in sequence must both survive —
    a fixed trash path would let the second overwrite the first."""
    c, ws, _rw, _ro = env
    (ws / "a").mkdir()
    (ws / "a" / "same.txt").write_text("first\n", encoding="utf-8")
    (ws / "b").mkdir()
    (ws / "b" / "same.txt").write_text("second\n", encoding="utf-8")

    assert _post(c, "delete", root="session", path="a/same.txt")["ok"] is True
    assert _post(c, "delete", root="session", path="b/same.txt")["ok"] is True

    bodies = sorted(p.read_text(encoding="utf-8") for p in ws.glob(".trash-*/same.txt"))
    assert bodies == ["first\n", "second\n"]


def test_a_delete_works_for_a_mounted_root_too(env):
    """A mount lives outside ``~/.ginno`` (usually another volume), so the
    fallback move has to survive a cross-filesystem copy — this is the case
    ``os.replace`` alone would fail."""
    c, _ws, rw, _ro = env
    (rw / "mounted.txt").write_text("mounted\n", encoding="utf-8")

    r = _post(c, "delete", root="f_rw", path="mounted.txt")

    assert r["ok"] is True
    assert not (rw / "mounted.txt").exists()
    assert r["trash_path"]
    assert "mounted.txt" in r["trash_path"]


# --------------------------------------------------------------------------- #
# check_only — the fence and the count, with no delete.
#
# This mode exists for the PACKAGED app: its delete ends in the Rust
# `code_trash` command, which only checks path containment and therefore cannot
# see mount tiers. And a per-root `writable` flag cannot stand in for the tier
# either, because `mount_access` is most-specific-match — a read-only mount
# nested inside a writable root is invisible when judged from the root. So this
# endpoint has to be the gate that path goes through.
# --------------------------------------------------------------------------- #
def test_check_only_reports_the_count_and_deletes_nothing(env):
    c, ws, _rw, _ro = env
    d = ws / "keep"
    d.mkdir()
    (d / "a.txt").write_text("a", encoding="utf-8")
    (d / "b.txt").write_text("b", encoding="utf-8")

    r = _post(c, "delete", root="session", path="keep", check_only=True)

    assert r["ok"] is True
    assert r["check_only"] is True
    assert r["count"] == 2
    # Nothing moved and no trash dir was even created — that is the point.
    assert (d / "a.txt").read_text(encoding="utf-8") == "a"
    assert (d / "b.txt").exists()
    assert not list(ws.glob(".trash-*"))


def test_check_only_still_enforces_the_read_only_tier(env):
    """The load-bearing assertion for the packaged delete path."""
    c, _ws, _rw, ro = env
    (ro / "nope").write_text("x", encoding="utf-8")

    r = _post(c, "delete", root="f_ro", path="nope", check_only=True)

    assert r["ok"] is False
    assert r["code"] == "read-only-mount"
    assert (ro / "nope").exists()


def test_check_only_still_refuses_a_path_outside_the_root(env):
    """The mode must not become a way to probe arbitrary paths."""
    c, _ws, _rw, _ro = env

    r = _post(c, "delete", root="session", path="../../etc/passwd", check_only=True)

    assert r["ok"] is False
    assert r["code"] == "outside-root"