"""Baseline disk persistence + ``GET /api/code/git?numstat=1`` (design §6.1, P2).

Pins:

* **roundtrip** — a snapshot for a session WITH a resolvable slug lands in
  ``projects/<slug>/sessions/<sid>/baselines.json`` and is restored lazily
  after the in-memory map is wiped (a restart simulation);
* **anonymous sessions stay memory-only** — without a slug no file is written
  (this is what keeps the pre-existing baselines unit tests disk-free);
* **corrupt disk file degrades** — garbage JSON answers ``baseline_exists:
  False`` instead of raising;
* **clear_session removes the disk file** — a deleted session must not
  resurrect its baselines;
* **numstat** — ``numstat=1`` adds per-file add/del counts (tracked-only,
  ROOT-relative via the same relocation as status); the default response has
  NO ``numstat`` key (backward compatible); a git failure omits the key
  (best-effort, never an error).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.files import baselines
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-baseline-disk"
Q = {"project_slug": SLUG, "session_id": SID}

_IDENT = {
    "GIT_AUTHOR_NAME": "ginno-test",
    "GIT_AUTHOR_EMAIL": "test@ginno.invalid",
    "GIT_COMMITTER_NAME": "ginno-test",
    "GIT_COMMITTER_EMAIL": "test@ginno.invalid",
}


@pytest.fixture(autouse=True)
def _clean_maps():
    baselines._BASELINES.clear()
    baselines._LOADED.clear()
    baselines._DISK_PATHS.clear()
    yield
    baselines._BASELINES.clear()
    baselines._LOADED.clear()
    baselines._DISK_PATHS.clear()


@pytest.fixture
def warm_session():
    """A warm session meta so ``_session_slug`` resolves without the index."""
    meta: dict = {"project_slug": SLUG, "context_dirs": []}
    _SESSIONS[SID] = meta
    yield meta
    _SESSIONS.pop(SID, None)


def _disk_file() -> Path:
    return paths.session_files_dir(SLUG, SID) / "baselines.json"


def _wipe_memory():
    """Simulate a process restart: memory map AND lazy-load marks cleared."""
    baselines._BASELINES.clear()
    baselines._LOADED.clear()


def _panel():
    app = FastAPI()
    app.include_router(code_panel.router)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# persistence roundtrip
# --------------------------------------------------------------------------- #
def test_roundtrip_through_disk(warm_session, tmp_path):
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "a.txt"
    f.write_text("v0\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, f)

    assert _disk_file().is_file()
    data = json.loads(_disk_file().read_text(encoding="utf-8"))
    assert data["schema"] == 1
    key = os.path.realpath(str(f))
    assert data["entries"][key]["content"] == "v0\n"

    _wipe_memory()
    assert baselines.get_baseline(SID, f)["content"] == "v0\n"


def test_diff_endpoint_after_memory_wipe(warm_session):
    """Acceptance: after a restart the /api/code/diff endpoint still answers
    with the baseline, restored from disk on demand."""
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "doc.txt"
    f.write_text("before\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, f)
    f.write_text("after\n", encoding="utf-8")
    _wipe_memory()

    client = _panel()
    r = client.get("/api/code/diff", params={**Q, "root": "session", "path": "doc.txt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["baseline_exists"] is True
    assert body["base_content"] == "before\n"
    assert body["current_content"] == "after\n"


def test_anonymous_session_stays_memory_only(tmp_path):
    """No resolvable slug → no disk write (keeps the classic unit tests pure)."""
    f = tmp_path / "x.txt"
    f.write_text("v0\n", encoding="utf-8")
    baselines.snapshot_before_write("anon-sid", f)
    assert baselines.get_baseline("anon-sid", f) is not None
    assert not (paths.home() / "projects").exists() or not list(
        (paths.home() / "projects").rglob("baselines.json")
    )


def test_corrupt_disk_file_degrades(warm_session):
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    _disk_file().write_text("{not json", encoding="utf-8")
    client = _panel()
    r = client.get("/api/code/diff", params={**Q, "root": "session", "path": "nope.txt"})
    assert r.status_code == 200, r.text
    assert r.json()["baseline_exists"] is False


def test_clear_session_removes_disk_file(warm_session):
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "a.txt"
    f.write_text("v0\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, f)
    assert _disk_file().is_file()
    baselines.clear_session(SID)
    assert not _disk_file().exists()
    _wipe_memory()
    assert baselines.get_baseline(SID, f) is None


def test_first_write_wins_across_reload(warm_session, tmp_path):
    """A restored baseline must not be overwritten by a later snapshot."""
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    f = ws / "a.txt"
    f.write_text("v0\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, f)
    _wipe_memory()
    baselines.get_baseline(SID, f)  # triggers lazy restore
    f.write_text("mutated\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, f)
    assert baselines.get_baseline(SID, f)["content"] == "v0\n"


# --------------------------------------------------------------------------- #
# numstat
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={**os.environ, **_IDENT},
    )


@pytest.fixture
def repo_panel(warm_session, tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path_factory.mktemp("repo")
    _git("init", "-q", cwd=repo)
    (repo / "keep.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    meta = warm_session
    meta["context_dirs"].append(
        {"id": "repo", "path": str(repo), "name": "repo", "access": "rw"}
    )
    client = _panel()
    return client, repo


def test_numstat_param(repo_panel):
    client, repo = repo_panel
    (repo / "mod.txt").write_text("a\nb\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02seed")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "add mod+bin", cwd=repo)
    (repo / "mod.txt").write_text("a\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02seed\x00changed")

    r = client.get("/api/code/git", params={**Q, "root": "repo", "numstat": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_repo"] is True
    ns = body["numstat"]
    assert ns["tracked_only"] is True
    assert ns["files"]["mod.txt"] == {"add": 0, "del": 1, "binary": False}
    assert ns["files"]["bin.dat"]["binary"] is True
    # status fields unchanged alongside
    assert "entries" in body


def test_numstat_default_absent(repo_panel):
    """Backward compat: without numstat=1 the response has no numstat key."""
    client, repo = repo_panel
    (repo / "mod.txt").write_text("changed\n", encoding="utf-8")
    r = client.get("/api/code/git", params={**Q, "root": "repo"})
    assert r.status_code == 200, r.text
    assert "numstat" not in r.json()


def test_numstat_git_failure_omits_key(repo_panel, monkeypatch):
    client, _repo = repo_panel
    monkeypatch.setattr(code_panel, "_git_run", lambda *a, **k: None)
    r = client.get("/api/code/git", params={**Q, "root": "repo", "numstat": 1})
    assert r.status_code == 200, r.text
    assert "numstat" not in r.json()


def test_numstat_subdir_root_relocates(repo_panel, tmp_path_factory):
    """A root that is a SUBDIRECTORY of the repo gets ROOT-relative numstat."""
    client, repo = repo_panel
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "keep.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "pkg", cwd=repo)
    (sub / "keep.txt").write_text("seed\nmore\n", encoding="utf-8")

    warm = _SESSIONS[SID]
    warm["context_dirs"].append(
        {"id": "sub", "path": str(sub), "name": "sub", "access": "rw"}
    )
    r = client.get("/api/code/git", params={**Q, "root": "sub", "numstat": 1})
    assert r.status_code == 200, r.text
    files = r.json()["numstat"]["files"]
    assert "keep.txt" in files  # NOT "pkg/keep.txt"
    assert files["keep.txt"] == {"add": 1, "del": 0, "binary": False}
