"""Saving from the code panel: ``PUT /api/code/write`` (brief §3.1, design §4.7-S2).

The write path is the one part of this panel that can destroy work, so each
test here pins a rule from the brief §1 rather than a code path:

* **Atomicity + permissions** — mkstemp makes 0600, and a bare rename would
  leave every save as 0600: a script loses ``+x`` and an ordinary file tightens
  from 0644. ``chmod``-before-``replace`` is what this file proves, by saving
  over a 0755 and a 0644 file and re-stat-ing.
* **Encoding is not silently swapped** — the file's encoding comes back from
  ``read`` and must travel back verbatim. A GBK file saved as UTF-8 still
  *looks* fine in the editor; the damage only shows on the next open, so the
  assertion here is on the raw bytes (they must still decode as GBK and NOT as
  UTF-8), not just on the round-tripped text.
* **Conflict without overwrite** — a stale ``base_version`` is a 409 carrying
  the version currently on disk, and the bytes on disk must be untouched. This
  is the "announce, never overwrite" rule; the agent editing the same file is
  the expected case, not the exotic one.

The ``ro`` / ``.git`` gates are owned by ``resolve_code_target(write=True)`` and
unit-tested in ``test_code_fence.py``; here they are only asserted through the
endpoint, to prove the endpoint actually calls the fence.

Sessions are warm (``_SESSIONS``) like ``test_code_raw.py``; ``isolated_home``
(tests/conftest.py, autouse) points ``$GINNO_HOME`` at a fresh tmp dir, and
``tmp_path_factory`` provides the mount dirs outside it.
"""

from __future__ import annotations

import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-write"
Q = {"project_slug": SLUG, "session_id": SID}

GBK_OLD = "第一行中文\n第二行\n"
GBK_NEW = "改过的内容\n中文仍然要用 GBK 存\n"


@pytest.fixture
def env(tmp_path_factory):
    """A warm session with an ``rw`` mount, an ``ro`` mount, and its workspace.

    All three roots are exercised: the session workspace (implicitly ``rw``) is
    where the ordinary save tests live, the mounts exist for the two write
    gates. The warm entry is removed afterwards so the module-level dict does
    not leak into other tests.
    """
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
    app.include_router(code_panel.router)
    try:
        yield TestClient(app), ws, rw, ro
    finally:
        _SESSIONS.pop(SID, None)


def _read(c: TestClient, root: str, path: str) -> dict:
    return c.get("/api/code/read", params={**Q, "root": root, "path": path}).json()


def _put(c: TestClient, root: str, path: str, content: str, base: str, **kw) -> dict:
    return c.put(
        "/api/code/write",
        json={
            **Q,
            "root": root,
            "path": path,
            "content": content,
            "base_version": base,
            **kw,
        },
    )


# --------------------------------------------------------------------------- #
# Happy path + the version token
# --------------------------------------------------------------------------- #
def test_write_saves_and_returns_the_post_write_version(env):
    """The returned version must come from a stat AFTER the write.

    Pinned by size, not by inequality: the new content is a different length, so
    a version computed before the write would carry the OLD size prefix. That
    matters because the client uses the value as its next ``base_version`` — a
    stale one would make the very next save a bogus conflict.
    """
    c, ws, _rw, _ro = env
    f = ws / "note.txt"
    f.write_text("hello\n", encoding="utf-8")
    v1 = _read(c, "session", "note.txt")["version"]
    assert v1.split(":")[0] == "6"

    new = "hello world, and a longer line\n"
    r = _put(c, "session", "note.txt", new, v1).json()

    assert r["ok"] is True
    assert r["version"].split(":")[0] == str(len(new.encode("utf-8")))
    assert f.read_bytes() == new.encode("utf-8")
    # The same version `read` now reports: write and read must not disagree
    # about what "version" means, or every subsequent save conflicts.
    assert _read(c, "session", "note.txt")["version"] == r["version"]

    # Having saved, the previous version is stale — the token really rotates.
    assert _put(c, "session", "note.txt", "x\n", v1).status_code == 409


def test_write_leaves_no_temp_file_behind(env):
    """The temp file is renamed away, not left next to the target.

    A stray ``.note.txt.XXXX.tmp`` in a git worktree would show up as an
    untracked file, and the tree would list it."""
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")
    v1 = _read(c, "session", "note.txt")["version"]

    assert _put(c, "session", "note.txt", "bye\n", v1).json()["ok"] is True

    assert sorted(p.name for p in ws.iterdir()) == ["note.txt"]


def test_write_creates_a_new_version_for_an_unchanged_body(env):
    """Saving identical text is still a save: it must not 409 against itself.

    The version is ``size:mtime_ns``, so re-writing the same bytes yields a new
    version (the file was genuinely replaced). The client simply adopts it."""
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("same\n", encoding="utf-8")
    v1 = _read(c, "session", "note.txt")["version"]

    r = _put(c, "session", "note.txt", "same\n", v1).json()

    assert r["ok"] is True
    assert (ws / "note.txt").read_text(encoding="utf-8") == "same\n"


# --------------------------------------------------------------------------- #
# Conflict: refuse, and carry the version that IS on disk
# --------------------------------------------------------------------------- #
def test_stale_base_version_conflicts_and_does_not_touch_the_file(env):
    """The agent edited the file under us → 409 + the on-disk version.

    Both halves matter: the 409 (so the UI can offer reload/diff/force) and the
    ``version`` field (so it can rebase without a second round trip). And the
    disk content must be EXACTLY what the other writer left — a "best effort"
    save here is the one unrecoverable bug this endpoint exists to prevent.
    """
    c, ws, _rw, _ro = env
    f = ws / "shared.py"
    f.write_text("print('mine')\n", encoding="utf-8")
    stale = _read(c, "session", "shared.py")["version"]

    # Someone else (the agent's edit_file) got there first.
    f.write_text("print('theirs')\n", encoding="utf-8")
    disk_bytes = f.read_bytes()
    disk_version = _read(c, "session", "shared.py")["version"]

    r = c.put(
        "/api/code/write",
        json={**Q, "root": "session", "path": "shared.py", "content": "print('clobber')\n",
              "base_version": stale},
    )

    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "conflict"
    assert body["version"] == disk_version
    assert body["message"]
    assert f.read_bytes() == disk_bytes  # nothing was written


def test_an_empty_base_version_conflicts_rather_than_counting_as_fresh(env):
    """A missing field must not be read as "no baseline, go ahead".

    The default is compare-and-fail: silently treating an absent ``base_version``
    as a free overwrite would make the whole mechanism opt-out from the client
    side."""
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")

    r = _put(c, "session", "note.txt", "overwritten\n", "")

    assert r.status_code == 409
    assert (ws / "note.txt").read_text(encoding="utf-8") == "hello\n"


# --------------------------------------------------------------------------- #
# Encoding: the file's encoding travels back untouched (brief §1-3)
# --------------------------------------------------------------------------- #
def test_gbk_file_saved_as_gbk_still_is_gbk(env):
    """Round-trip a GBK file and assert on the BYTES, not the text.

    Decoding the file back as UTF-8 would be the silent-corruption failure, and
    it is invisible to a text-level assertion (the editor shows mojibake only on
    the next open). So: the raw bytes must decode as GBK *and* must fail to
    decode as UTF-8 — which is what proves the encoding echoed by ``read`` was
    actually used, instead of a hardcoded utf-8.
    """
    c, ws, _rw, _ro = env
    f = ws / "gbk.txt"
    f.write_bytes(GBK_OLD.encode("gbk"))

    rd = _read(c, "session", "gbk.txt")
    assert rd["encoding"] == "gbk"
    assert rd["text"] == GBK_OLD

    r = _put(c, "session", "gbk.txt", GBK_NEW, rd["version"], encoding="gbk").json()
    assert r["ok"] is True

    raw = f.read_bytes()
    assert raw.decode("gbk") == GBK_NEW
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    # And a fresh read still reports it as GBK with the right text.
    rd2 = _read(c, "session", "gbk.txt")
    assert rd2["encoding"] == "gbk"
    assert rd2["text"] == GBK_NEW


def test_a_gbk_file_cannot_be_saved_with_content_it_cannot_represent(env):
    """An emoji has no GBK form: refuse, and say it is an encoding problem.

    The alternative (falling back to UTF-8) rewrites the file's encoding behind
    the user's back; the alternative (errors="replace") corrupts it silently.
    """
    c, ws, _rw, _ro = env
    f = ws / "gbk.txt"
    f.write_bytes(GBK_OLD.encode("gbk"))
    rd = _read(c, "session", "gbk.txt")

    r = _put(c, "session", "gbk.txt", "笑脸 😀\n", rd["version"], encoding="gbk").json()

    assert r["ok"] is False
    assert r["code"] == "not-text"
    assert "gbk" in r["message"]
    assert f.read_bytes().decode("gbk") == GBK_OLD  # untouched, still valid GBK


def test_an_unknown_encoding_is_refused_not_ignored(env):
    """A codec name we cannot use must fail loudly — never silently mean utf-8."""
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]

    r = _put(c, "session", "note.txt", "hello\n", v, encoding="not-a-codec").json()

    assert r["ok"] is False
    assert r["code"] == "not-text"


def test_utf8_default_when_encoding_is_omitted(env):
    """``encoding`` is optional on the wire and defaults to utf-8 (brief §3.1)."""
    c, ws, _rw, _ro = env
    f = ws / "note.txt"
    f.write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]

    assert _put(c, "session", "note.txt", "héllo — 中文\n", v).json()["ok"] is True

    assert f.read_bytes() == "héllo — 中文\n".encode("utf-8")


# --------------------------------------------------------------------------- #
# Permissions survive the atomic replace (brief §1-4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", [0o755, 0o644, 0o600])
def test_write_preserves_the_permission_bits(env, mode):
    """mkstemp makes 0600; the rename must not leave that behind.

    0755 is the case with a visible symptom (a script stops being runnable),
    0644 is the common case (every save would tighten a normal file), and 0600
    is the control — it is what a naive implementation produces, so it proves
    the assertion is really reading the file's mode.
    """
    c, ws, _rw, _ro = env
    f = ws / "run.sh"
    f.write_text("echo old\n", encoding="utf-8")
    f.chmod(mode)
    v = _read(c, "session", "run.sh")["version"]

    assert _put(c, "session", "run.sh", "echo new\n", v).json()["ok"] is True

    assert stat.S_IMODE(f.stat().st_mode) == mode
    assert f.read_text(encoding="utf-8") == "echo new\n"


# --------------------------------------------------------------------------- #
# Fence: the endpoint must walk the SAME gates as `read`
# --------------------------------------------------------------------------- #
def test_write_into_an_ro_mount_is_refused(env):
    """``ro`` is a hard constraint, not a UI hint — the request never lands."""
    c, _ws, _rw, ro = env
    f = ro / "no.txt"
    f.write_text("readable\n", encoding="utf-8")
    v = _read(c, "f_ro", "no.txt")["version"]  # reads are fine from an ro mount

    r = _put(c, "f_ro", "no.txt", "written\n", v).json()

    assert r["ok"] is False
    assert r["code"] == "read-only-mount"
    assert f.read_text(encoding="utf-8") == "readable\n"


def test_write_inside_git_is_refused(env):
    """Nothing under ``.git`` is ever writable, even on an rw mount.

    The path is still READABLE (the tree shows ``.git`` with "show all" on), so
    this is a write-only gate and the test asserts both sides."""
    c, _ws, rw, _ro = env
    (rw / ".git").mkdir()
    cfg = rw / ".git" / "config"
    cfg.write_text("[core]\n", encoding="utf-8")

    assert _read(c, "f_rw", ".git/config")["ok"] is True
    r = _put(c, "f_rw", ".git/config", "[core]\n\tbare = true\n", "0:0").json()

    assert r["ok"] is False
    assert r["code"] == "denied-path"
    assert cfg.read_text(encoding="utf-8") == "[core]\n"


def test_write_refuses_an_unknown_root_and_a_traversal(env):
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]

    unknown = _put(c, "f_ghost", "note.txt", "x\n", v).json()
    assert unknown["ok"] is False and unknown["code"] == "unknown-root"

    escape = _put(c, "session", "../../../etc/passwd", "x\n", v).json()
    assert escape["ok"] is False and escape["code"] == "outside-root"


def test_write_to_an_unknown_session_is_refused(env):
    c, _ws, _rw, _ro = env
    r = c.put(
        "/api/code/write",
        json={**Q, "session_id": "ghost", "root": "session", "path": "a.txt",
              "content": "x", "base_version": "0:0"},
    ).json()

    assert r["ok"] is False
    assert r["code"] == "unknown-root"


# --------------------------------------------------------------------------- #
# Shape of the target
# --------------------------------------------------------------------------- #
def test_a_deleted_file_is_absent(env):
    """Concurrent delete (the agent removed the file we had open).

    ``absent`` and not ``not-text``: the UI's move is "the file is gone — close
    the tab / recreate it", which is different copy from "that is not a file".
    (Creating files is S4; in S2 a path that is not on disk cannot be written.)"""
    c, ws, _rw, _ro = env
    f = ws / "gone.txt"
    f.write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "gone.txt")["version"]
    f.unlink()

    r = _put(c, "session", "gone.txt", "hello again\n", v).json()

    assert r["ok"] is False
    assert r["code"] == "absent"
    assert not f.exists()  # the endpoint did not helpfully recreate it


def test_a_directory_is_not_text(env):
    c, ws, _rw, _ro = env
    (ws / "sub").mkdir()

    r = _put(c, "session", "sub", "x\n", "0:0").json()

    assert r["ok"] is False
    assert r["code"] == "not-text"


def test_content_must_be_a_string(env):
    """A missing/non-string ``content`` must not be coerced (``None`` would
    raise inside the endpoint and surface as a 500)."""
    c, ws, _rw, _ro = env
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]

    r = c.put(
        "/api/code/write",
        json={**Q, "root": "session", "path": "note.txt", "base_version": v},
    ).json()

    assert r["ok"] is False
    assert r["code"] == "not-text"
    assert (ws / "note.txt").read_text(encoding="utf-8") == "hello\n"


# --------------------------------------------------------------------------- #
# Size cap
# --------------------------------------------------------------------------- #
def test_content_over_the_editable_cap_is_refused(env):
    """The same ceiling ``read`` uses: a file too big to edit is too big to
    write. Checked on the ENCODED length, i.e. what would land on disk."""
    c, ws, _rw, _ro = env
    f = ws / "note.txt"
    f.write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]

    r = _put(c, "session", "note.txt", "a" * (code_panel.EDITABLE_MAX_BYTES + 1), v).json()

    assert r["ok"] is False
    assert r["code"] == "too-large"
    assert f.read_text(encoding="utf-8") == "hello\n"
    # Nothing was staged: a rejected write must not leave a temp file either.
    assert sorted(p.name for p in ws.iterdir()) == ["note.txt"]


def test_a_multibyte_file_at_the_cap_is_measured_in_bytes(env):
    """Non-ASCII text is capped by its encoded byte length, so a body that is
    under the cap in characters but over it in bytes is still refused."""
    c, ws, _rw, _ro = env
    f = ws / "note.txt"
    f.write_text("hello\n", encoding="utf-8")
    v = _read(c, "session", "note.txt")["version"]
    # 1/3 of the cap in characters, but 3 bytes each → over it.
    body = "中" * (code_panel.EDITABLE_MAX_BYTES // 3 + 1)

    r = _put(c, "session", "note.txt", body, v).json()

    assert r["ok"] is False
    assert r["code"] == "too-large"
    assert f.read_text(encoding="utf-8") == "hello\n"