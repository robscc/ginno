"""API tests: Settings → 会话文件 management endpoints (session-scoped dirs)."""

from __future__ import annotations

import subprocess

import pytest

from ginno_runtime import artifacts as art_store
from ginno_runtime import paths
from ginno_runtime.files import get_registry, reset_registries
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def _fresh(isolated_home):
    reset_registries()
    yield
    reset_registries()


@pytest.fixture
def sid(client, patch_build_model, tmp_path):
    patch_build_model(script(text="ok"))
    ws = tmp_path / "ws"
    ws.mkdir()
    r = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": str(ws), "agent_id": "dev", "title": "T"},
    ).json()
    assert r["ok"] is True
    return r["id"]


def _upload(client, sid, name="a.csv", data=b"a,b\n1,2\n"):
    r = client.post(
        "/api/files", data={"session_id": sid}, files={"file": (name, data, "text/csv")}
    ).json()
    assert r["ok"] is True
    return r["file"]


def test_create_session_makes_files_dir(client, sid):
    d = paths.session_files_dir("default", sid)
    assert d.is_dir()


def test_dirs_lists_session_then_orphaned_after_delete(client, sid):
    _upload(client, sid)
    r = client.get("/api/session-files/dirs").json()
    assert r["ok"] is True
    mine = next(s for s in r["sessions"] if s["session_id"] == sid)
    assert mine["orphaned"] is False
    assert mine["title"] == "T"
    assert mine["file_count"] == 1

    # delete the session → files dir preserved, now flagged orphaned
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
    assert paths.session_files_dir("default", sid).is_dir()  # preserved
    r2 = client.get("/api/session-files/dirs").json()
    mine2 = next(s for s in r2["sessions"] if s["session_id"] == sid)
    assert mine2["orphaned"] is True
    assert mine2["file_count"] == 1


def test_list_files_in_session_dir(client, sid):
    _upload(client, sid, "one.csv")
    _upload(client, sid, "two.csv")
    r = client.get(
        f"/api/session-files/list?project_slug=default&session_id={sid}&sub=uploads"
    ).json()
    assert r["ok"] is True
    names = {e["name"] for e in r["entries"]}
    assert len(names) == 2
    assert all(e["type"] == "file" for e in r["entries"])
    # root listing shows uploads/ (and maybe results/) as dirs
    root = client.get(
        f"/api/session-files/list?project_slug=default&session_id={sid}"
    ).json()
    assert any(e["type"] == "dir" and e["name"] == "uploads" for e in root["entries"])


def test_delete_file_refused_for_active_session(client, sid):
    f = _upload(client, sid, "live.csv")
    rel = f["path"].split(f"/sessions/{sid}/", 1)[1]
    r = client.request(
        "DELETE",
        "/api/session-files/file",
        json={"project_slug": "default", "session_id": sid, "path": rel},
    ).json()
    assert r["ok"] is False  # active session's files are protected
    from pathlib import Path

    assert Path(f["path"]).exists()


def test_delete_dir_refused_for_active_session(client, sid):
    _upload(client, sid, "live.csv")
    r = client.request(
        "DELETE",
        "/api/session-files/dir",
        json={"project_slug": "default", "session_id": sid},
    ).json()
    assert r["ok"] is False
    assert paths.session_files_dir("default", sid).exists()


def test_delete_file_removes_disk_registry_artifact(client, sid):
    f = _upload(client, sid, "del.csv")
    reg = get_registry("default")
    entry = reg.find_by_path(f["path"])
    art_id = entry["artifact_id"]
    rel = f["path"].split(f"/sessions/{sid}/", 1)[1]

    # only deletable once the session is deleted (orphaned)
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
    r = client.request(
        "DELETE",
        "/api/session-files/file",
        json={"project_slug": "default", "session_id": sid, "path": rel},
    ).json()
    assert r["ok"] is True and r["unregistered"] is True
    from pathlib import Path

    assert not Path(f["path"]).exists()
    assert reg.find_by_path(f["path"]) is None
    assert art_store.get_artifact("default", art_id) is None
    # gone from GET /api/files too
    listed = client.get("/api/files?project_slug=default").json()
    assert all(x["id"] != entry["id"] for x in listed)


def test_delete_whole_session_dir(client, sid):
    _upload(client, sid, "x.csv")
    _upload(client, sid, "y.csv")
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True  # orphan first
    r = client.request(
        "DELETE",
        "/api/session-files/dir",
        json={"project_slug": "default", "session_id": sid},
    ).json()
    assert r["ok"] is True and r["files_removed"] == 2
    assert not paths.session_files_dir("default", sid).exists()
    assert client.get("/api/files?project_slug=default").json() == []


def test_path_traversal_rejected(client, sid, isolated_home):
    # try to escape the session dir and touch settings.json
    r = client.request(
        "DELETE",
        "/api/session-files/file",
        json={"project_slug": "default", "session_id": sid, "path": "../../../settings.json"},
    ).json()
    assert r["ok"] is False
    assert (isolated_home / "settings.json").exists()  # untouched
    # malformed session_id
    r2 = client.get("/api/session-files/list?project_slug=default&session_id=../evil").json()
    assert r2["ok"] is False


def test_reveal_monkeypatched(client, sid, monkeypatch):
    f = _upload(client, sid, "reveal.csv")
    rel = f["path"].split(f"/sessions/{sid}/", 1)[1]
    calls = []

    class _P:
        def __init__(self, args, *a, **k):
            calls.append(list(args))

    monkeypatch.setattr(subprocess, "Popen", _P)
    r = client.post(
        "/api/session-files/reveal",
        json={"project_slug": "default", "session_id": sid, "path": rel},
    ).json()
    assert r["ok"] is True
    assert calls and calls[0][0] == "open" and calls[0][1] == "-R"


def test_cleanup_orphans_removes_only_orphan_dirs(client, sid):
    """一键清理:孤儿目录整目录删除,活跃会话目录不受影响。"""
    from ginno_runtime import server_shared

    _upload(client, sid)  # live session keeps its files

    # 手造一个孤儿目录(会话不在索引里)——绕过 delete_session 使其可控
    orphan_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    orphan_dir = paths.session_files_dir("default", orphan_id)
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "leftover.txt").write_text("x" * 100)

    r = client.post("/api/session-files/cleanup-orphans").json()
    assert r["ok"] is True
    assert [x["session_id"] for x in r["removed"]] == [orphan_id]
    assert r["freed_bytes"] >= 100
    assert not orphan_dir.exists()
    # 活跃会话的目录原封不动
    assert paths.session_files_dir("default", sid).is_dir()

    # 再跑一次:没有孤儿 → 空清理
    r2 = client.post("/api/session-files/cleanup-orphans").json()
    assert r2["ok"] is True and r2["removed"] == [] and r2["freed_bytes"] == 0
