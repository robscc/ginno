"""Git decoration for the code panel: ``GET /api/code/git`` + ``read?rev=HEAD``.

The endpoint is *decoration*, so most of what these tests pin is the degradation
contract rather than the happy path (brief §1-3): a root outside any repository,
a machine with no ``git`` on ``PATH``, or a git call that fails must all answer
``is_repo:false`` / ``entries:{}`` with HTTP 200 and **never raise**. Real
temporary repositories are used throughout — mocking ``git`` would assert the
parser against my own idea of porcelain=v2 rather than against git's.

Two failure modes get their own, deliberately strong tests:

* **Relocation.** ``git status`` reports TOPLEVEL-relative paths while every
  other endpoint (and the tree) speaks ROOT-relative paths. A root that is a
  subdirectory of the repository is therefore the easiest thing to get wrong,
  and the untracked/renamed files prove it either way.
* **The ``rev=HEAD`` baseline on a subdir root.** ``git show HEAD:<path>`` also
  resolves from the toplevel, so S1 shipped with this silently broken whenever
  the root was not the repo top. The test plants a decoy file of the SAME name
  at the toplevel: without the prefix the read returns the decoy's content,
  which a "does it return something" assertion would happily accept.

The rename test additionally guards a porcelain=v2 ``-z`` quirk: a renamed
record carries its original path as the NEXT NUL-separated field, which a naive
parser reads as another record (the source file here is named like one, so the
bug surfaces as a spurious entry).

Sessions are warm (``_SESSIONS``) like ``test_code_raw.py``; ``isolated_home``
(tests/conftest.py, autouse) points ``$GINNO_HOME`` at a fresh tmp dir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-git"
Q = {"project_slug": SLUG, "session_id": SID}

# Deterministic identity: `git commit` refuses without one, and the developer's
# global config must not leak into the assertion on the branch name.
_IDENT = {
    "GIT_AUTHOR_NAME": "ginno-test",
    "GIT_AUTHOR_EMAIL": "test@ginno.invalid",
    "GIT_COMMITTER_NAME": "ginno-test",
    "GIT_COMMITTER_EMAIL": "test@ginno.invalid",
}


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in a temp repo, isolated from the host's config."""
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={**os.environ, **_IDENT},
    )


def _init_repo(d: Path) -> Path:
    """A fresh repository with no commits."""
    d.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=d)
    return d


def _commit_all(d: Path, message: str = "init") -> None:
    _git("add", "-A", cwd=d)
    _git("commit", "-qm", message, cwd=d)


@pytest.fixture
def panel(tmp_path_factory):
    """A warm session whose roots the test mounts itself, plus a `mount` helper.

    ``mount()`` appends a context dir *after* the client exists, which works
    because every request re-reads the session meta — so one fixture covers the
    repo, subdir, and non-repo roots without three near-identical fixtures.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    meta: dict = {"project_slug": SLUG, "context_dirs": []}
    _SESSIONS[SID] = meta
    app = FastAPI()
    app.include_router(code_panel.router)
    client = TestClient(app)

    def mount(d: Path, rid: str, access: str = "rw") -> str:
        meta["context_dirs"].append(
            {"id": rid, "path": str(d), "name": rid, "access": access}
        )
        return rid

    try:
        yield client, mount
    finally:
        _SESSIONS.pop(SID, None)


def _git_body(c: TestClient, root: str) -> dict:
    r = c.get("/api/code/git", params={**Q, "root": root})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# The happy path: every status letter, at a toplevel root
# --------------------------------------------------------------------------- #
def test_git_reports_m_added_d_renamed_and_ignored(panel, tmp_path_factory):
    """M / A(untracked) / D / R / ! as the brief's acceptance list requires.

    The root IS the repository toplevel here, so paths pass through unchanged —
    the relocation case is the next test.
    """
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo"))
    (repo / "m.txt").write_text("one\n")
    (repo / "d.txt").write_text("two\n")
    (repo / "old name.txt").write_text("three\n")
    (repo / "keep.txt").write_text("untouched\n")
    (repo / ".gitignore").write_text("ign/\n")
    _commit_all(repo)

    (repo / "m.txt").write_text("one changed\n")  # staged-or-not modification
    (repo / "d.txt").unlink()                     # deleted in the worktree
    _git("mv", "old name.txt", "new name.txt", cwd=repo)  # staged rename
    (repo / "u.txt").write_text("brand new\n")    # untracked
    ignored = repo / "ign"
    ignored.mkdir()
    (ignored / "x.txt").write_text("ignored\n")
    mount(repo, "f_repo")

    body = _git_body(c, "f_repo")

    assert body["ok"] is True and body["is_repo"] is True
    assert Path(body["toplevel"]).resolve() == repo.resolve()
    assert body["branch"] == "main"
    assert body["entries"] == {
        "m.txt": "M",
        "d.txt": "D",
        "new name.txt": "R",
        # The rename's ORIGINAL path must not show up as a second, invented file
        # (nor as "old name.txt"): the tree shows the target only.
        "u.txt": "A",
        # A whole ignored directory arrives as "ign/" and is keyed without the
        # trailing slash so it matches the tree's own path spelling.
        "ign": "!",
    }


def test_a_rename_record_does_not_swallow_the_next_record(panel, tmp_path_factory):
    """Porcelain=v2 ``-z`` type-``2`` records carry the ORIGINAL path as the next
    NUL-separated field.

    The source file is named ``? ghost.txt`` on purpose: a parser that fails to
    consume that extra field reads it AS a record and invents an untracked entry
    for ``ghost.txt``. So the assertion below is really "the field was
    consumed", expressed as the absence of a file that does not exist.
    """
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-rename"))
    (repo / "? ghost.txt").write_text("payload\n")
    (repo / "sibling.txt").write_text("sibling\n")
    _commit_all(repo)
    _git("mv", "? ghost.txt", "renamed.txt", cwd=repo)
    mount(repo, "f_repo")

    body = _git_body(c, "f_repo")

    assert body["entries"].get("renamed.txt") == "R"
    assert "ghost.txt" not in body["entries"]
    assert "? ghost.txt" not in body["entries"]


def test_an_untouched_repository_has_no_entries(panel, tmp_path_factory):
    """A clean tree is an empty map, not an error and not a "" letter."""
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-clean"))
    (repo / "a.txt").write_text("a\n")
    _commit_all(repo)
    mount(repo, "f_repo")

    body = _git_body(c, "f_repo")

    assert body["is_repo"] is True and body["entries"] == {}


# --------------------------------------------------------------------------- #
# Relocation — the easiest thing here to get wrong
# --------------------------------------------------------------------------- #
def test_paths_are_relocated_to_a_subdirectory_root(panel, tmp_path_factory):
    """A root BELOW the toplevel must see root-relative keys and nothing else.

    The identical modifications outside the root are the control: if the offset
    were dropped (or applied twice) they would leak into — or replace — the
    inside-root entries.
    """
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-sub"))
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "m.txt").write_text("one\n")
    (pkg / "old.txt").write_text("three\n")
    (repo / "outside.txt").write_text("out\n")
    _commit_all(repo)

    (pkg / "m.txt").write_text("changed\n")      # M, inside the root
    (pkg / "u.txt").write_text("new\n")          # A, inside the root
    _git("mv", "old.txt", "new.txt", cwd=pkg)    # R, inside the root
    (repo / "outside.txt").write_text("out 2\n")  # M, OUTSIDE the root
    mount(pkg, "f_pkg")

    body = _git_body(c, "f_pkg")

    assert Path(body["toplevel"]).resolve() == repo.resolve()
    assert body["entries"] == {"m.txt": "M", "u.txt": "A", "new.txt": "R"}
    assert "outside.txt" not in body["entries"]
    # No leaked toplevel-relative spelling either.
    assert "pkg/m.txt" not in body["entries"]


# --------------------------------------------------------------------------- #
# Degradation — the endpoint must never be the reason something breaks
# --------------------------------------------------------------------------- #
def test_a_non_repository_degrades_to_is_repo_false(panel, tmp_path_factory, monkeypatch):
    """Not a repo → the documented empty payload, HTTP 200, no error code.

    ``GIT_CEILING_DIRECTORIES`` stops git from walking up out of the temp dir
    into some ancestor repository, which would otherwise make this test depend
    on where pytest's tmp dir happens to live.
    """
    c, mount = panel
    plain = tmp_path_factory.mktemp("plain")
    (plain / "a.txt").write_text("hi\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(plain.parent))
    mount(plain, "f_plain")

    body = _git_body(c, "f_plain")

    assert body == {
        "ok": True,
        "is_repo": False,
        "toplevel": None,
        "branch": None,
        "entries": {},
    }


def test_a_missing_git_binary_degrades_silently(panel, tmp_path_factory, monkeypatch):
    """No ``git`` on ``PATH`` is the same answer as "no repository".

    ``PATH`` is emptied so ``execvp`` cannot find the binary — the subprocess
    raises before git ever runs, which is exactly the environment the endpoint
    has to survive. ``TestClient`` re-raises server exceptions, so an escaping
    ``FileNotFoundError`` would fail this test rather than the assertion.
    """
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-nogit"))
    (repo / "m.txt").write_text("one\n")
    _commit_all(repo)
    (repo / "m.txt").write_text("changed\n")
    mount(repo, "f_repo")

    monkeypatch.setenv("PATH", "")

    body = _git_body(c, "f_repo")
    assert body["is_repo"] is False and body["entries"] == {}

    # `read?rev=HEAD` goes through git too and must degrade the same way, with
    # the ordinary {ok:false} envelope rather than a 500.
    r = c.get(
        "/api/code/read",
        params={**Q, "root": "f_repo", "path": "m.txt", "rev": "HEAD"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["code"] == "absent"


def test_git_reports_an_unknown_root_and_session(panel):
    """The fence is shared: an unreachable root/session is the usual {ok:false},
    not an empty-but-successful git payload."""
    c, _mount = panel

    bad_root = _git_body(c, "f_nope")
    assert bad_root["ok"] is False and bad_root["code"] == "unknown-root"

    r = c.get(
        "/api/code/git",
        params={"project_slug": SLUG, "session_id": "ghost", "root": "session"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["code"] == "unknown-root"


# --------------------------------------------------------------------------- #
# `read?rev=HEAD` baseline (the S1 gap the brief asks to close)
# --------------------------------------------------------------------------- #
def test_read_head_uses_the_toplevel_prefix_on_a_subdir_root(panel, tmp_path_factory):
    """The baseline must be the root's file, not the toplevel's same-named one.

    ``repo/f.txt`` is the decoy: the OLD code ran ``git show HEAD:f.txt`` from
    the subdir, which resolves ``f.txt`` at the TOPLEVEL and returns the decoy.
    Asserting the exact content — rather than merely ``ok`` — is what pins the
    fix.
    """
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-head"))
    pkg = repo / "pkg"
    pkg.mkdir()
    (repo / "f.txt").write_text("toplevel decoy\n")
    (pkg / "f.txt").write_text("committed pkg\n")
    _commit_all(repo)
    (pkg / "f.txt").write_text("changed in the worktree\n")
    mount(pkg, "f_pkg")

    head = c.get(
        "/api/code/read",
        params={**Q, "root": "f_pkg", "path": "f.txt", "rev": "HEAD"},
    ).json()
    work = c.get(
        "/api/code/read", params={**Q, "root": "f_pkg", "path": "f.txt"}
    ).json()

    assert head["ok"] is True
    assert head["text"] == "committed pkg\n"
    assert work["text"] == "changed in the worktree\n"


def test_read_head_works_when_the_root_is_the_toplevel(panel, tmp_path_factory):
    """The common case keeps working: offset is "" and the path is unchanged."""
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-head-top"))
    (repo / "f.txt").write_text("committed\n")
    _commit_all(repo)
    (repo / "f.txt").write_text("changed\n")
    mount(repo, "f_repo")

    head = c.get(
        "/api/code/read", params={**Q, "root": "f_repo", "path": "f.txt", "rev": "HEAD"}
    ).json()

    assert head["ok"] is True and head["text"] == "committed\n"


def test_read_head_is_absent_for_an_untracked_file(panel, tmp_path_factory):
    """A file git never saw has no HEAD blob; that is a normal miss, not an
    error — the diff view shows it as wholly new."""
    c, mount = panel
    repo = _init_repo(tmp_path_factory.mktemp("repo-head-new"))
    (repo / "a.txt").write_text("a\n")
    _commit_all(repo)
    (repo / "fresh.txt").write_text("new\n")
    mount(repo, "f_repo")

    r = c.get(
        "/api/code/read",
        params={**Q, "root": "f_repo", "path": "fresh.txt", "rev": "HEAD"},
    ).json()

    assert r["ok"] is False and r["code"] == "absent"