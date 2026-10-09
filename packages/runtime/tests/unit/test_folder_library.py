"""Folder library unit tests — auto_mount flag semantics (home-mount-picker-
design.md §2.2): the flag is library-level, patchable, and pre-fills the home
composer's pending mounts on new sessions."""

from __future__ import annotations

import pytest

from ginno_runtime import context_folders as cf

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    yield


def _mk(dir_name: str) -> str:
    d = cf.paths.home() / dir_name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def test_add_folder_default_has_no_auto_mount():
    f = cf.add_folder(_mk("a"))
    assert f.get("auto_mount") is False


def test_add_folder_with_auto_mount_persists_flag():
    f = cf.add_folder(_mk("b"), auto_mount=True)
    assert f["auto_mount"] is True
    assert cf.get_folder(f["id"])["auto_mount"] is True


def test_readd_same_path_updates_auto_mount_idempotently():
    p = _mk("c")
    f1 = cf.add_folder(p)
    assert f1["auto_mount"] is False
    # 幂等再入库：同路径返回同一条目，字段以新值胜出
    f2 = cf.add_folder(p, auto_mount=True)
    assert f2["id"] == f1["id"]
    assert f2["auto_mount"] is True
    # 再以原默认值调用不会把标记洗掉？——会：updated fields win，
    # 这是 create_folder 透传 auto_mount 时的既定语义(显式传 False 即取消)。
    f3 = cf.add_folder(p, auto_mount=False)
    assert f3["auto_mount"] is False


def test_update_folder_whitelist_accepts_auto_mount():
    f = cf.add_folder(_mk("d"))
    patched = cf.update_folder(f["id"], {"auto_mount": True})
    assert patched["auto_mount"] is True
    # 白名单外的字段照旧被忽略
    patched = cf.update_folder(f["id"], {"evil": "x"})
    assert "evil" not in patched
