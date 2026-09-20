"""Unit tests for auto-discovered project roots (projects.py).

The behaviour under test is what makes the 2026-09-17 ambiguity visible: a
repo the agent only ever reached by absolute path — never mounted — must be
recognizable, and Ginno's own tree must never be mistaken for a project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime import paths, projects

pytestmark = pytest.mark.unit


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    """A work dir OUTSIDE Ginno's home, which is the realistic layout.

    GINNO_HOME and the user's repos must not be nested — putting the test
    repos under the isolated home would (correctly) get them refused as
    "Ginno's own tree", which is the invariant test_ginnos_own_tree pins.
    """
    home = tmp_path / "ginno-home"
    home.mkdir()
    monkeypatch.setenv("GINNO_HOME", str(home))
    w = tmp_path / "work"
    w.mkdir()
    return w


def _repo(root: Path, marker: str = ".git") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if marker == ".git":
        (root / ".git").mkdir(exist_ok=True)
    else:
        (root / marker).write_text("# rules\n", encoding="utf-8")
    return root


def _skill(repo: Path, name: str) -> Path:
    d = repo / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n\nx\n")
    return d


# --------------------------------------------------------------------------- #
# detect_root
# --------------------------------------------------------------------------- #
def test_finds_repo_root_from_a_deep_file(tmp_path, work):
    repo = _repo(work / "claude-agent-team")
    deep = repo / "apps" / "web" / "src" / "main.ts"
    deep.parent.mkdir(parents=True)
    deep.write_text("x")
    root, markers = projects.detect_root(deep)
    assert root == repo and ".git" in markers


def test_nearest_root_wins(tmp_path, work):
    outer = _repo(work / "outer")
    inner = _repo(outer / "packages" / "inner")
    f = inner / "a.py"
    f.write_text("x")
    root, _ = projects.detect_root(f)
    assert root == inner


def test_claude_markers_and_skill_count(tmp_path, work):
    repo = work / "proj"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text("# hi\n")
    _skill(repo, "ponytail")
    _skill(repo, "brainstorming")
    # The file need not exist: bash is scanned before it runs, so this is the
    # "about to scaffold into an existing repo" shape too.
    root, markers = projects.detect_root(repo / "src" / "x.ts")
    assert root == repo and "CLAUDE.md" in markers
    entry = projects.record("default", "s1", repo / "src" / "x.ts")
    assert entry["has_claude"] is True and entry["claude_skills"] == 2


def test_relative_and_empty_paths_are_refused(tmp_path, work):
    assert projects.detect_root("some/rel/path") is None
    assert projects.detect_root("") is None
    assert projects.detect_root("   ") is None


def test_path_that_is_being_created(tmp_path, work):
    """`bash "mkdir -p <work>/new-repo"` is scanned before the dir exists —
    detection must not depend on it being there yet."""
    repo = _repo(work / "new-repo")
    assert projects.detect_root(work / "new-repo" / "src") == (repo, [".git"])


def test_ginnos_own_tree_is_never_a_project(tmp_path, work):
    """The session workspace and ~/.ginno/skills live under home — recording
    either as "the user's project" would point install_skills at Ginno."""
    home = paths.home()
    assert home.name == "ginno-home"  # the fixture really moved GINNO_HOME
    assert projects.detect_root(home) is None
    ws = paths.session_files_dir("default", "abc123")
    ws.mkdir(parents=True, exist_ok=True)
    assert projects.detect_root(ws) is None
    assert projects.detect_root(paths.global_skills_dir()) is None


def test_stops_at_a_filesystem_root(tmp_path, work):
    """Walking up from a markerless path must not record "/" or /tmp."""
    d = work / "no-markers-anywhere" / "deep"
    d.mkdir(parents=True)
    assert projects.detect_root(d) is None


def test_depth_cap(tmp_path, work):
    """A marker ABOVE the depth cap is out of reach."""
    repo = _repo(work / "r")
    deep = repo
    for i in range(projects.MAX_DEPTH + 2):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    assert projects.detect_root(deep) is None


# --------------------------------------------------------------------------- #
# record / known
# --------------------------------------------------------------------------- #
def test_record_returns_entry_only_when_new(tmp_path, work):
    repo = _repo(work / "r")
    f = repo / "a.txt"
    f.write_text("x")
    first = projects.record("default", "s1", f)
    assert first is not None and first["path"] == str(repo)
    # The once-only signal: an already-known root must not re-announce itself
    # on every single file read.
    assert projects.record("default", "s1", f) is None


def test_record_needs_a_session(tmp_path, work):
    repo = _repo(work / "r")
    assert projects.record("default", "", repo) is None
    assert projects.record("", "s1", repo) is None


def test_known_is_newest_first_and_persists(tmp_path, work):
    a = _repo(work / "a")
    b = _repo(work / "b")
    projects.record("default", "s1", a)
    projects.record("default", "s1", b)
    got = [p["path"] for p in projects.known("default", "s1")]
    assert got == [str(b), str(a)]
    # Persisted, not in-memory: a reload sees the same list.
    assert [p["path"] for p in projects.known("default", "s1")] == got
    assert projects.registry_path("default", "s1").exists()


def test_registry_is_per_session(tmp_path, work):
    repo = _repo(work / "r")
    projects.record("default", "s1", repo)
    assert projects.known("default", "s2") == []


def test_cap_drops_oldest(tmp_path, work):
    repos = [_repo(work / f"r{i}") for i in range(projects.MAX_ROOTS + 2)]
    for r in repos:
        projects.record("default", "s1", r)
    kept = [p["path"] for p in projects.known("default", "s1")]
    assert len(kept) == projects.MAX_ROOTS
    assert str(repos[0]) not in kept  # oldest evicted
    assert str(repos[-1]) in kept


def test_corrupt_registry_degrades_to_empty(tmp_path, work):
    p = projects.registry_path("default", "s1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert projects.known("default", "s1") == []


def test_forget(tmp_path, work):
    repo = _repo(work / "r")
    projects.record("default", "s1", repo)
    projects.forget("default", "s1")
    assert projects.known("default", "s1") == []


def test_claude_skills_dir():
    assert str(projects.claude_skills_dir("/tmp/x")) == "/tmp/x/.claude/skills"