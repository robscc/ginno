"""Unit tests for the code panel's security fence (docs/code-panel-design.md §4.6).

``resolve_code_target`` is the single gate every ``/api/code/*`` request walks
through, and it is the one part of the code panel that is security-critical
(writes land on the same resolver in S2). The design doc required a unit test
for gate 4's "exact mirror" rule; before this file the behaviour was only
covered by a throwaway script, so nothing failed if a refactor swapped an
argument.

Everything here uses REAL temp directories — no filesystem mocking. Sessions
are seeded on disk (``folders.json`` + ``sessions/_index.json``) so the tests
exercise the same cold-session resolution path ``_ensure_session`` uses;
``isolated_home`` (tests/conftest.py, autouse) points ``$GINNO_HOME`` at a
fresh tmp dir, and ``tmp_path_factory`` provides directories OUTSIDE that home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.api.code import CodeAccessError, resolve_code_target
from ginno_runtime.tools.builtin import _path_denied

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s1"


def _seed_session(
    home: Path,
    mounts: list[tuple[str, Path, str]] | None = None,
    *,
    primary_id: str | None = None,
    slug: str = SLUG,
    sid: str = SID,
) -> dict:
    """Seed a COLD session on disk and resolve its code-panel context.

    ``mounts`` are ``(folder_id, path, access)`` triples written to
    ``folders.json`` (the same library ``api/folders.py`` manages); the session
    index row carries only the ids + ``primary_folder``, so ``_mount_dirs``
    takes the ``resolve_session_dirs`` path a never-loaded session takes.
    """
    mounts = mounts or []
    home.joinpath("folders.json").write_text(
        json.dumps(
            {
                "folders": [
                    {
                        "id": fid,
                        "path": str(p),
                        "name": p.name,
                        "access": access,
                    }
                    for fid, p, access in mounts
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    sessions = paths.project_sessions_dir(slug)
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "_index.json").write_text(
        json.dumps(
            [
                {
                    "id": sid,
                    "project_slug": slug,
                    "context_folders": [fid for fid, _, _ in mounts],
                    "primary_folder": primary_id,
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    paths.session_files_dir(slug, sid).mkdir(parents=True, exist_ok=True)
    ctx = code_panel._session_ctx(slug, sid)
    # A None here means the meta was not found — every assertion below would
    # then be vacuous, so fail loudly instead.
    assert ctx is not None, "cold-session meta must resolve from _index.json"
    return ctx


def _seed_root(ctx: dict, root_id: str, relpath: str, *, write: bool = False) -> Path:
    """Call the resolver and return the resolved path (raises on a gate)."""
    target, _root = resolve_code_target(ctx, root_id, relpath, write=write)
    return target


# --------------------------------------------------------------------------- #
# 1. THE REGRESSION TEST — gate 4's base_dir is the SESSION WORKSPACE.
# --------------------------------------------------------------------------- #
def test_session_root_allowed_when_primary_folder_is_outside_home(tmp_path, tmp_path_factory):
    """A session whose ``primary_folder`` lives OUTSIDE ``~/.ginno`` must still
    be able to browse its own workspace root.

    This is the bug the design doc §4.6 / brief §1-1 calls out: ``_path_denied``
    only exempts a ``base_dir`` that is a PROPER subdirectory of ``paths.home()``
    (``~/.ginno``). The session workspace qualifies; a mounted ``primary_folder``
    does NOT. So passing ``primary_path`` as the exempt base silently turns the
    whole session workspace into a denied region — the visible symptom is an
    empty "会话工作区" root.
    """
    outside = tmp_path_factory.mktemp("primary-outside")
    ctx = _seed_session(tmp_path, [("f_out", outside, "rw")], primary_id="f_out")
    ws = Path(ctx["workspace"])

    # The premise of the test, asserted rather than assumed: workspace is under
    # home, the primary mount is not.
    assert ws == paths.session_files_dir(SLUG, SID)
    assert ws.is_relative_to(paths.home())
    assert not Path(ctx["primary_path"]).is_relative_to(paths.home())

    note = ws / "note.md"
    note.write_text("hello", encoding="utf-8")

    # (a) The session root is reachable: the root itself AND a real file in it.
    assert _seed_root(ctx, "session", "") == ws
    target, root = resolve_code_target(ctx, "session", "note.md")
    assert root["id"] == "session"
    assert target == note
    assert target.read_text(encoding="utf-8") == "hello"

    # (b) Counterfactual — passing the WRONG base_dir (primary_path) DENIES it.
    # This is why the assertion above is meaningful: it proves the argument
    # choice in resolve_code_target is load-bearing, and a future refactor that
    # swaps `session["workspace"]` for `primary_path` fails loudly here.
    assert _path_denied(target, Path(ctx["primary_path"]), ctx["mount_roots"]) is True
    # ... and the CORRECT base_dir (the session workspace) exempts it.
    assert _path_denied(target, ws, ctx["mount_roots"]) is False


# --------------------------------------------------------------------------- #
# 2. mount_roots must NOT contain the session workspace.
# --------------------------------------------------------------------------- #
def test_mount_roots_never_include_the_session_workspace(tmp_path, tmp_path_factory):
    """``extra_roots`` is mounts-only — see the comment at builtin.py:243-247.

    ``_path_denied`` exempts anything inside an ``extra_roots`` entry BEFORE it
    even looks at the home deny. The session workspace is exempted separately,
    and only when it is a proper subdir of home. Putting it in ``extra_roots``
    would punch a hole through the home deny whenever ``workspace == home`` (the
    workflow cwd fallback) — every checkpoint, settings file and memory under
    ``~/.ginno`` would become reachable.
    """
    outside = tmp_path_factory.mktemp("mount-for-mountroots")
    ctx = _seed_session(tmp_path, [("f_out", outside, "rw")], primary_id="f_out")

    ws = Path(ctx["workspace"])
    assert ws not in [Path(p) for p in ctx["mount_roots"]]
    # The mount set is exactly the mounted dirs, nothing more.
    assert [Path(p) for p in ctx["mount_roots"]] == [outside.resolve()]


def test_workspace_in_mount_roots_would_punch_through_the_home_deny(tmp_path):
    """The hazard the previous test pins, demonstrated directly.

    With ``workspace == home`` (the workflow cwd fallback) ``_path_denied``
    correctly denies ``~/.ginno/settings.json``; add the workspace to
    ``extra_roots`` and the same path is allowed, because the extra_roots loop
    short-circuits before the home deny.
    """
    home = paths.home()
    ws = home  # the cwd fallback shape
    secret = home / "settings.json"
    secret.write_text("top secret", encoding="utf-8")

    assert _path_denied(secret, ws, []) is True
    assert _path_denied(secret, ws, [ws]) is False  # <- the hole


# --------------------------------------------------------------------------- #
# 3. Traversal / containment.
# --------------------------------------------------------------------------- #
def test_relative_traversal_is_refused(tmp_path):
    ctx = _seed_session(tmp_path, [])
    ws = Path(ctx["workspace"])
    # Real files above the root, so the refusal is about containment and not
    # about the target being missing.
    (ws.parent / "secret.txt").write_text("secret", encoding="utf-8")
    (ws.parent.parent / "secret.txt").write_text("secret2", encoding="utf-8")
    # Enough `..` to clear ~/.ginno entirely — without it a traversal "escape"
    # could still be caught by the home deny and the test would pass for the
    # wrong reason (wrong error code) instead of proving the boundary holds.
    outside_home = "../" * (len(ws.relative_to(paths.home()).parts) + 1) + "etc/passwd"

    for rel in ("../secret.txt", "../../secret.txt", "../../etc/passwd", outside_home):
        with pytest.raises(CodeAccessError) as ei:
            resolve_code_target(ctx, "session", rel)
        assert ei.value.code == "outside-root", rel


def test_absolute_path_as_relative_path_is_refused(tmp_path, tmp_path_factory):
    """The client sends root_id + relative path, NEVER an absolute path.

    ``_norm_rel`` strips the leading ``/``, so the containment check has to look
    at the ORIGINAL argument (``Path(relpath).is_absolute()``) — the normalized
    echo alone would let an absolute path through looking like a relative one.
    """
    outside = tmp_path_factory.mktemp("abs-target")
    victim = outside / "abs.txt"
    victim.write_text("x", encoding="utf-8")
    ctx = _seed_session(tmp_path, [])

    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "session", str(victim))
    assert ei.value.code == "outside-root"


def test_symlink_escaping_the_root_is_refused(tmp_path, tmp_path_factory):
    """Gate 3: containment must be re-checked AFTER dereferencing symlinks.

    The lexical path (``link.txt``) is inside the root, so gate 2 passes; only
    the resolved target escapes. Without the post-resolution re-check a symlink
    is a trivially open door out of every root.
    """
    outside = tmp_path_factory.mktemp("symlink-target")
    (outside / "target.txt").write_text("outside", encoding="utf-8")
    ctx = _seed_session(tmp_path, [])
    ws = Path(ctx["workspace"])

    link = ws / "link.txt"
    link.symlink_to(outside / "target.txt")
    assert link.is_relative_to(ws)  # purely lexical: it *looks* contained
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "session", "link.txt")
    assert ei.value.code == "outside-root"

    # Same for a symlinked DIRECTORY: the escape shows up in a parent component.
    linkdir = ws / "linkdir"
    linkdir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "session", "linkdir/target.txt")
    assert ei.value.code == "outside-root"


def test_symlink_within_the_root_is_allowed(tmp_path):
    """Positive control for gate 3: it dereferences, it does not blanket-ban.

    Design §1.3 deliberately relaxed dsh's "reject every symlink" rule —
    real repositories are full of in-tree links, so a contained link must
    resolve to its target rather than fail.
    """
    ctx = _seed_session(tmp_path, [])
    ws = Path(ctx["workspace"])
    real = ws / "real.txt"
    real.write_text("inner", encoding="utf-8")
    (ws / "alias.txt").symlink_to(real)

    target = _seed_root(ctx, "session", "alias.txt")
    assert target == real
    assert target.read_text(encoding="utf-8") == "inner"


# --------------------------------------------------------------------------- #
# 4. denied-path: a mounted ~/.ssh appears as a root but is never readable.
# --------------------------------------------------------------------------- #
def test_ssh_mount_is_listed_but_every_file_is_denied(tmp_path, tmp_path_factory, monkeypatch):
    """Acceptance §4.8-6: mounting ``~/.ssh`` lists it as a root, by design,
    because the roots list is presentation. Containment alone would make it
    readable (the file IS inside a root) — the permanent deny regions must be
    layered ON TOP of containment (§4.6: "闸门 4 是为什么'只做包含性检查不够'").
    """
    fake_user_home = Path(tmp_path_factory.mktemp("fakeuser").resolve())
    ssh = fake_user_home / ".ssh"
    ssh.mkdir()
    (ssh / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_user_home))  # ~/.ssh now resolves here

    ctx = _seed_session(tmp_path, [("f_ssh", ssh, "rw")], primary_id="f_ssh")

    # Half one: the root IS listed (and keeps its declared access tier).
    roots = {r["id"]: r for r in code_panel._list_roots(ctx)}
    assert "f_ssh" in roots
    assert roots["f_ssh"]["path"] == str(ssh)
    assert roots["f_ssh"]["access"] == "rw"

    # Half two: resolving anything inside it — the file, or the root itself —
    # fails with denied-path, even though the mount is "rw".
    for rel in ("id_rsa", ""):
        with pytest.raises(CodeAccessError) as ei:
            resolve_code_target(ctx, "f_ssh", rel, write=True)
        assert ei.value.code == "denied-path", rel


# --------------------------------------------------------------------------- #
# 5. Write gates (resolver only — S1 has no write endpoint).
# --------------------------------------------------------------------------- #
def test_write_gates(tmp_path, tmp_path_factory):
    rw = tmp_path_factory.mktemp("rw-mount")
    ro = tmp_path_factory.mktemp("ro-mount")
    ctx = _seed_session(tmp_path, [("f_rw", rw, "rw"), ("f_ro", ro, "ro")])
    (rw / "ok.txt").write_text("x", encoding="utf-8")
    (ro / "no.txt").write_text("x", encoding="utf-8")
    (rw / ".git").mkdir()
    (rw / ".git" / "config").write_text("x", encoding="utf-8")

    # A normal file in an rw mount resolves for write...
    assert _seed_root(ctx, "f_rw", "ok.txt", write=True) == rw / "ok.txt"
    # ...and the session workspace (implicitly rw) does too.
    ws = Path(ctx["workspace"])
    (ws / "out.txt").write_text("x", encoding="utf-8")
    assert _seed_root(ctx, "session", "out.txt", write=True) == ws / "out.txt"

    # An ``ro`` mount is a hard constraint (independent of bypass_permissions)...
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "f_ro", "no.txt", write=True)
    assert ei.value.code == "read-only-mount"
    # ...but reads from it are fine: the tier only gates writes.
    assert _seed_root(ctx, "f_ro", "no.txt", write=False) == ro / "no.txt"

    # Nothing under .git is ever writable, even on an rw mount and even with
    # "show all" on — and the write gate reports it as denied-path.
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "f_rw", ".git/config", write=True)
    assert ei.value.code == "denied-path"
    # The same path is readable (the tree shows .git when "show all" is on).
    assert _seed_root(ctx, "f_rw", ".git/config", write=False) == rw / ".git" / "config"


# --------------------------------------------------------------------------- #
# 6. Unknown root / ghost session.
# --------------------------------------------------------------------------- #
def test_unknown_root_id_is_refused(tmp_path):
    ctx = _seed_session(tmp_path, [])
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "f_ghost", "")
    assert ei.value.code == "unknown-root"


def test_missing_root_dir_is_root_missing(tmp_path, tmp_path_factory):
    """A mount whose directory disappeared (moved/deleted) is ``root-missing``,
    not ``unknown-root`` — it is on the root list, flagged ``missing``."""
    parent = tmp_path_factory.mktemp("vanishing")
    gone = parent / "sub"  # registered as a mount, then moved away
    assert not gone.exists()
    ctx = _seed_session(tmp_path, [("f_gone", gone, "rw")])

    roots = {r["id"]: r for r in code_panel._list_roots(ctx)}
    assert roots["f_gone"]["missing"] is True
    with pytest.raises(CodeAccessError) as ei:
        resolve_code_target(ctx, "f_gone", "")
    assert ei.value.code == "root-missing"


async def test_ghost_session_endpoint_returns_unknown_root():
    """A session id that resolves nowhere yields the wire code, HTTP 200, so the
    panel can render copy instead of an error page (`{ok, ...}` house style)."""
    out = await code_panel.code_roots("default", "no-such-session")
    assert out["ok"] is False
    assert out["code"] == "unknown-root"

    listing = await code_panel.code_list("default", "no-such-session", "session", "")
    assert listing["ok"] is False and listing["code"] == "unknown-root"