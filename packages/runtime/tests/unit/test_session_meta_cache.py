"""Regression tests for cached reads of the on-disk session index."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import session_meta as sm
from ginno_runtime.api.sessions.relations import router


@pytest.fixture
def index_path() -> Path:
    path = sm.paths.session_index_path("default")
    path.parent.mkdir(parents=True)
    return path


@pytest.fixture
def index_loads(monkeypatch):
    loads = Mock(wraps=sm.json.loads)
    monkeypatch.setattr(
        sm, "json", SimpleNamespace(
            loads=loads, dumps=sm.json.dumps, JSONDecodeError=sm.json.JSONDecodeError,
        ),
    )
    return loads


def _write_rows(path: Path, *rows: dict) -> None:
    path.write_text(sm.json.dumps(rows), encoding="utf-8")


def test_linked_requests_parse_unchanged_index_once(index_path, index_loads):
    _write_rows(index_path, {"id": "source", "linked_user": ["target"]}, {"id": "target"})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for _ in range(3):
            response = client.get("/api/sessions/source/linked")
            assert response.status_code == 200
            assert [row["id"] for row in response.json()["members"]] == ["target"]
    assert index_loads.call_count == 1


def test_external_atomic_replace_with_same_size_and_mtime(index_path):
    _write_rows(index_path, {"id": "before"})
    assert sm._session_meta_list("default") == [{"id": "before"}]
    before = index_path.stat()
    replacement = index_path.with_suffix(".swap")
    _write_rows(replacement, {"id": "after!"})
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(replacement, index_path)
    after = index_path.stat()
    assert after.st_ino != before.st_ino
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert sm._session_meta_list("default") == [{"id": "after!"}]


def test_external_inplace_edit_restoring_mtime(index_path, monkeypatch):
    _write_rows(index_path, {"id": "before"})
    assert sm._session_meta_list("default") == [{"id": "before"}]
    before = index_path.stat()
    _write_rows(index_path, {"id": "after!"})
    os.utime(index_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = index_path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino, before.st_size, before.st_mtime_ns,
    )

    # Report a changed ctime even on filesystems that round both writes to the
    # same tick. The inode, mtime, size and bytes still come from the real file.
    real_stat = Path.stat

    def stat_with_changed_ctime(path, *args, **kwargs):
        stat = real_stat(path, *args, **kwargs)
        if path == index_path:
            return SimpleNamespace(
                st_dev=stat.st_dev,
                st_ino=stat.st_ino,
                st_size=stat.st_size,
                st_mtime_ns=stat.st_mtime_ns,
                st_ctime_ns=before.st_ctime_ns + 1,
            )
        return stat

    monkeypatch.setattr(Path, "stat", stat_with_changed_ctime)
    assert sm._session_meta_list("default") == [{"id": "after!"}]


def test_cache_version_tracks_the_file_actually_opened(index_path, monkeypatch):
    original, replacement = (index_path.with_suffix(suffix) for suffix in (".a", ".b"))
    _write_rows(original, {"id": "original"})
    _write_rows(replacement, {"id": "replacement"})
    index_path.symlink_to(original)
    new_link = index_path.with_suffix(".new")
    old_link = index_path.with_suffix(".old")
    new_link.symlink_to(replacement)
    old_link.symlink_to(original)
    real_open = Path.open

    def open_between_replacements(path, *args, **kwargs):
        if path != index_path:
            return real_open(path, *args, **kwargs)
        # Change the path between stat and open, then restore the original
        # target's exact version before the reader observes the path again.
        os.replace(new_link, path)
        try:
            return real_open(path, *args, **kwargs)
        finally:
            os.replace(old_link, path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_between_replacements)
        assert sm._session_meta_list("default") == [{"id": "replacement"}]
    assert sm._session_meta_list("default") == [{"id": "original"}]


def test_missing_between_stat_and_open_then_recovered(index_path, monkeypatch):
    _write_rows(index_path, {"id": "before"})
    real_open = Path.open

    def open_after_removal(path, *args, **kwargs):
        if path == index_path:
            path.unlink()
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_after_removal)
        assert sm._session_meta_list("default") == []
    assert sm._session_meta_list("default") == []
    _write_rows(index_path, {"id": "recreated"})
    assert sm._session_meta_list("default") == [{"id": "recreated"}]


@pytest.mark.parametrize("corrupt", [b"{broken", b"\xff"], ids=["json", "utf8"])
def test_corrupt_index_is_retried_and_recovers(index_path, index_loads, corrupt):
    _write_rows(index_path, {"id": "before"})
    assert sm._session_meta_list("default") == [{"id": "before"}]
    index_path.write_bytes(corrupt)
    assert sm._session_meta_list("default") == []
    assert sm._session_meta_list("default") == []
    assert index_loads.call_count == 3
    _write_rows(index_path, {"id": "repaired"})
    assert sm._session_meta_list("default") == [{"id": "repaired"}]
    assert index_loads.call_count == 4


def test_internal_writes_invalidate_and_keep_patch_semantics(index_path, index_loads):
    row = {"id": "sid", "title": "before", "updated": 42, "labels": ["keep"]}
    _write_rows(index_path, row)
    assert sm._session_meta_list("default") == [row]
    result = sm._session_meta_patch("default", "sid", {"title": "after", "labels": None}, touch=False)
    expected = dict(row, title="after")
    assert result == expected
    result["labels"].clear()
    assert sm._session_meta_list("default") == [expected]
    assert index_loads.call_count == 2

    sm._session_meta_update_all("default", lambda _: {"provider": "new"})
    assert sm._session_meta_list("default") == [dict(expected, provider="new")]
    sm._session_meta_rewrite_all("default", [{"id": "rewritten"}])
    assert sm._session_meta_list("default") == [{"id": "rewritten"}]
    assert sm._session_meta_remove("default", "rewritten")
    assert sm._session_meta_list("default") == []


def test_failed_patch_leaves_disk_and_cached_snapshot_intact(index_path, index_loads, monkeypatch):
    initial = {"id": "sid", "title": "before"}
    _write_rows(index_path, initial)
    assert sm._session_meta_list("default") == [initial]
    real_replace = os.replace

    def fail_index_replace(source, dest):
        if dest == index_path:
            raise PermissionError("simulated index write failure")
        return real_replace(source, dest)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_index_replace)
        with pytest.raises(PermissionError, match="simulated index write failure"):
            sm._session_meta_patch("default", "sid", {"title": "failed"}, touch=False)
    assert sm._session_meta_list("default") == [initial]
    assert index_loads.call_count == 1
    sm._session_meta_patch("default", "sid", {"title": "retried"}, touch=False)
    assert sm._session_meta_list("default") == [dict(initial, title="retried")]


def test_index_read_permission_error_is_retryable(index_path, monkeypatch):
    _write_rows(index_path, {"id": "available"})
    real_stat = Path.stat

    def permission_denied(path, *args, **kwargs):
        if path == index_path:
            raise PermissionError("simulated index read failure")
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", permission_denied)
        with pytest.raises(PermissionError, match="simulated index read failure"):
            sm._session_meta_list("default")
    assert sm._session_meta_list("default") == [{"id": "available"}]


def test_cache_isolated_by_ginno_home_and_project(tmp_path, monkeypatch, index_loads):
    cases = [
        (tmp_path / "home-a", "default", "a"),
        (tmp_path / "home-a", "other", "b"),
        (tmp_path / "home-b", "default", "c"),
    ]
    for home, slug, sid in cases:
        monkeypatch.setenv("GINNO_HOME", str(home))
        path = sm.paths.session_index_path(slug)
        path.parent.mkdir(parents=True)
        _write_rows(path, {"id": sid})
    for home, slug, sid in cases * 2:
        monkeypatch.setenv("GINNO_HOME", str(home))
        assert sm._session_meta_list(slug) == [{"id": sid}]
    assert index_loads.call_count == len(cases)


def test_mutating_a_read_cannot_change_nested_cached_data(index_path, index_loads):
    row = {
        "id": "sid",
        "labels": ["keep"],
        "subagent": {"status": "running"},
        "linked_auto": [{"id": "peer", "rel": "assigned"}],
    }
    _write_rows(index_path, row)
    first = sm._session_meta_list("default")
    first[0]["title"] = "bad"
    first[0]["labels"].append("bad")
    first[0]["subagent"]["status"] = "bad"
    first[0]["linked_auto"][0]["rel"] = "bad"
    first.append({"id": "ghost"})
    assert sm._session_meta_list("default") == [row]
    assert index_loads.call_count == 1
