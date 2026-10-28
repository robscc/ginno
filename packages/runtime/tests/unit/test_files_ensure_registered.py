"""Unit: POST /api/files/ensure-registered — attach_ref ref_id → registry entry.

老会话历史回放出的 attach_ref chip 从未注册过文件（live 转向在 ref.emit 时
才注册），点击时经此端点按路径登记（幂等、不拷贝）拿到预览所需的 file id。
相对 ref 按模型 ref 的同一规则归一到 session workspace（见
test_artifact_ref_heal 的 2026-08 事故）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime import paths
from ginno_runtime.api import files as files_api
from ginno_runtime.files import get_registry, reset_registries
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_registry(isolated_home):
    _SESSIONS.clear()
    reset_registries()
    yield
    _SESSIONS.clear()
    reset_registries()


def _seed_session(sid: str) -> Path:
    ws = paths.session_files_dir("default", sid)
    ws.mkdir(parents=True, exist_ok=True)
    _SESSIONS[sid] = {
        "session_id": sid,
        "project_slug": "default",
        "workspace": str(ws),
    }
    return ws


async def test_registers_absolute_path_idempotently(tmp_path):
    _seed_session("s1")
    f = tmp_path / "下单链路优化机会.md"
    f.write_text("# ok", encoding="utf-8")

    r1 = await files_api.ensure_registered_endpoint(
        {"session_id": "s1", "path": str(f), "name": "下单链路优化机会.md（已补）"}
    )
    assert r1["ok"] and r1["file"]["path"] == str(f)
    assert r1["file"]["kind"] == "text"  # 按扩展名归类，SheetViewer 路由用
    assert r1["file"]["name"] == "下单链路优化机会.md（已补）"

    # 幂等：同一路径重复登记命中同一 entry，不新增
    r2 = await files_api.ensure_registered_endpoint(
        {"session_id": "s1", "path": str(f)}
    )
    assert r2["file"]["id"] == r1["file"]["id"]
    assert len(get_registry("default").list_all()) == 1


async def test_relative_ref_resolves_against_session_workspace():
    ws = _seed_session("s2")
    (ws / "report.html").write_text("<html/>", encoding="utf-8")

    r = await files_api.ensure_registered_endpoint(
        {"session_id": "s2", "path": "report.html"}
    )
    assert r["ok"]
    assert r["file"]["path"] == str((ws / "report.html").resolve())


async def test_missing_file_and_unknown_session_fail_soft():
    _seed_session("s3")
    r = await files_api.ensure_registered_endpoint(
        {"session_id": "s3", "path": "/no/such/file.md"}
    )
    assert r["ok"] is False and "not found" in r["error"]
    assert get_registry("default").list_all() == []

    r = await files_api.ensure_registered_endpoint(
        {"session_id": "ghost", "path": "/tmp/x.md"}
    )
    assert r["ok"] is False and "session not found" in r["error"]
