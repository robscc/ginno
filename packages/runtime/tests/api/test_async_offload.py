"""Exercise the heavy operation itself through the ASGI route."""

from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from functools import wraps
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from starlette.responses import FileResponse

from ginno_runtime import paths, providers, server, settings_store
from ginno_runtime import jsonx as json
from ginno_runtime.api import export as export_api
from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.files import get_registry, reset_registries
from ginno_runtime.session_meta import _session_meta_upsert

pytestmark = pytest.mark.api


def _trace_loop(monkeypatch, owner, name, *, when=lambda *_a, **_kw: True):
    """Record execution context and delegate to the real implementation."""
    states = []
    original = getattr(owner, name)

    @wraps(original)
    def probe(*args, **kwargs):
        if when(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                states.append(False)
            else:
                states.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, probe)
    return states


@pytest.fixture
def inline_file(isolated_home):
    reset_registries()
    source = isolated_home / "preview.html"
    body = "<!DOCTYPE html><p>你好</p>".encode()
    source.write_bytes(body)
    row = get_registry("default").register(source.name, source, kind="html")
    yield Path(row["path"]), body, f"/api/files/{row['id']}/inline"
    reset_registries()


def test_inline_reads_in_worker_and_keeps_preview_headers(client, inline_file, monkeypatch):
    source, body, url = inline_file
    states = _trace_loop(monkeypatch, Path, "read_bytes", when=lambda p: p == source)
    response = client.get(url, headers={"Range": "bytes=0-1"})
    assert response.status_code == 200
    assert response.content == body
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == (
        "sandbox allow-scripts; "
        "default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval'; "
        "connect-src 'none'; form-action 'none'"
    )
    assert "content-disposition" not in response.headers
    assert "content-range" not in response.headers
    assert states == [False]


def test_inline_missing_read_keeps_structured_404(client, inline_file, monkeypatch):
    source, _, url = inline_file
    read_bytes = Path.read_bytes

    def fail_for_preview(path):
        if path == source:
            raise FileNotFoundError(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_for_preview)
    response = client.get(url)
    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "File has been moved or deleted"}


def test_session_export_reads_real_checkpoint_in_worker(client, monkeypatch):
    sid, slug, title = "export-test", "archive-project", "渠道/会话"
    _session_meta_upsert(slug, {"id": sid, "title": title, "channel": "wecom"})
    cp = empty_checkpoint()
    cp["channel_values"] = {"messages": [HumanMessage("导出正文"), AIMessage("答复正文")]}
    FileCheckpointer(slug).put({"configurable": {"thread_id": sid}}, cp, {}, {})
    lookups = _trace_loop(monkeypatch, export_api, "_find_meta")
    reads = _trace_loop(monkeypatch, FileCheckpointer, "get_tuple")

    response = client.get(f"/api/export/session/{sid}", params={"format": "markdown"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f"attachment; filename*=UTF-8''{quote('渠道_会话')}.md"
    )
    assert response.text.startswith(f"# {title}\n\n- 会话：`{sid}`")
    assert response.text.endswith("## 用户\n\n导出正文\n\n## 助手\n\n答复正文\n")
    assert lookups == reads == [False]


def test_project_export_compresses_in_worker_and_keeps_manifest(client, monkeypatch):
    project = paths.project_dir("archive-project")
    project.mkdir()
    source = project / "sample.dat"
    source.write_bytes(b"opaque data\0")
    states = _trace_loop(monkeypatch, zipfile.ZipFile, "write")

    response = client.post("/api/export/project", json={"project_slug": project.name})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="archive-project-')
    assert response.headers["accept-ranges"] == "bytes"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "archive-project/sample.dat", "archive-project/EXPORT_MANIFEST.json",
        }
        assert archive.read("archive-project/sample.dat") == b"opaque data\0"
        manifest = json.loads(archive.read("archive-project/EXPORT_MANIFEST.json"))
        assert manifest["project_slug"] == project.name
        assert manifest["source"] == str(project)
        assert manifest["exported_at"] > 0
    assert states == [False]


async def test_same_second_project_exports_keep_separate_downloads(monkeypatch):
    project = paths.project_dir("archive-project")
    project.mkdir(parents=True)
    source = project / "sample.dat"
    source.write_bytes(b"first")
    monkeypatch.setattr(
        export_api, "time", SimpleNamespace(strftime=lambda _fmt: "20260101-000000", time=lambda: 1),
    )
    first_ready = asyncio.Event()
    send_first = asyncio.Event()
    send_file = FileResponse.__call__

    async def intercept_send(self, scope, receive, send):
        if not first_ready.is_set():
            first_ready.set()
            await send_first.wait()
        await send_file(self, scope, receive, send)

    monkeypatch.setattr(FileResponse, "__call__", intercept_send)
    request = {"project_slug": project.name}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/export/project", json=request))
        try:
            await asyncio.wait_for(first_ready.wait(), 5)
            source.write_bytes(b"second")
            second = await client.post("/api/export/project", json=request)
        finally:
            send_first.set()
            first = await first
    for response, expected in ((first, b"first"), (second, b"second")):
        assert response.status_code == 200
        assert response.headers["content-disposition"] == (
            'attachment; filename="archive-project-20260101-000000.zip"'
        )
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.read("archive-project/sample.dat") == expected


async def test_project_export_keeps_destination_parent_for_nested_slug():
    project_slug = "group/sample"
    paths.project_dir(project_slug).mkdir(parents=True)
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/export/project", json={"project_slug": project_slug})
        assert response.status_code == 500

        parent = paths.home() / "index" / "exports" / "group"
        parent.mkdir()
        response = await client.post("/api/export/project", json={"project_slug": project_slug})
        assert response.status_code == 200
        (archive,) = parent.glob("sample-*.zip")
        assert response.content == archive.read_bytes()


async def test_project_export_io_error_does_not_leave_partial_archive(monkeypatch):
    paths.project_dir("archive-project").mkdir(parents=True)

    def fail_manifest(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_manifest)
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/export/project", json={"project_slug": "archive-project"})
    assert response.status_code == 500
    out_dir = paths.home() / "index" / "exports"
    assert list(out_dir.iterdir()) == []


def test_memory_summary_and_pool_decode_records_in_worker(client, monkeypatch):
    pool = paths.memory_pool_dir()
    pool.mkdir(parents=True, exist_ok=True)
    source = pool / "001.jsonl"
    source.write_text(json.dumps({
        "timestamp": 1, "session_id": "s", "agent_id": "dev", "cited": True, "content": "回顾",
    }) + "\n", encoding="utf-8")
    states = _trace_loop(monkeypatch, Path, "read_text", when=lambda p, **_: p == source)

    summary = client.get("/api/memory")
    assert summary.status_code == 200
    assert summary.json()["pool_count"] == 1
    response = client.get("/api/memory/pool", params={"limit": 1})
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "total": 1, "cited": 1, "first_ts": 1.0, "last_ts": 1.0,
        "entries": [{
            "timestamp": 1, "session_id": "s", "agent_id": "dev", "cited": True,
            "chars": 2, "excerpt": "回顾",
        }],
    }
    assert states == [False, False]


def test_memory_runs_reads_full_log_in_worker(client, monkeypatch):
    source = paths.memory_runs_path()
    rows = [{"ts": 10, "run_id": "old"}, {"ts": 20, "run_id": "new"}]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    states = _trace_loop(monkeypatch, Path, "read_text", when=lambda p, **_: p == source)
    response = client.get("/api/memory/runs", params={"limit": 1})
    assert response.status_code == 200
    assert response.json()["runs"] == [rows[1]]
    assert states == [False]


def test_settings_masks_response_in_worker_and_restores_secret_on_put(client, monkeypatch):
    token = "synthetic-token-for-settings-1234"
    settings_store.update(lambda doc: {**doc, "env": {"MY_API_TOKEN": token}})
    states = _trace_loop(monkeypatch, providers, "mask_settings")
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["env"]["MY_API_TOKEN"] == providers.mask_secret(token)
    assert token not in response.text
    assert states == [False]

    assert client.put("/api/settings", json=response.json()).json() == {"ok": True}
    assert settings_store.load_doc().data["env"]["MY_API_TOKEN"] == token


def test_mcp_config_decodes_in_worker(client, monkeypatch):
    cfg = {"mcpServers": {"example": {"command": "example", "enabled": False}}}
    source = paths.mcp_config_path()
    source.write_text(json.dumps(cfg), encoding="utf-8")
    states = _trace_loop(monkeypatch, Path, "read_text", when=lambda p, **_: p == source)
    response = client.get("/api/mcp/config")
    assert response.status_code == 200
    assert response.json() == cfg
    assert states == [False]


async def test_mcp_config_reader_finishes_before_rest_writer(monkeypatch):
    old = {"mcpServers": {"old": {"command": "old"}}}
    new = {"mcpServers": {"new": {"command": "new"}}}
    source = paths.mcp_config_path()
    source.write_text(json.dumps(old), encoding="utf-8")
    loop = asyncio.get_running_loop()
    reader_waiting = asyncio.Event()
    writer_sent_body = asyncio.Event()
    unblock_reader = Event()
    read_text = Path.read_text

    def pause_reader(path, *args, **kwargs):
        if path == source:
            loop.call_soon_threadsafe(reader_waiting.set)
            assert unblock_reader.wait(5)
        return read_text(path, *args, **kwargs)

    async def put_body():
        yield json.dumps(new).encode()
        writer_sent_body.set()

    monkeypatch.setattr(Path, "read_text", pause_reader)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read = asyncio.create_task(client.get("/api/mcp/config"))
        try:
            await asyncio.wait_for(reader_waiting.wait(), 5)
            write = asyncio.create_task(client.put(
                "/api/mcp", content=put_body(), headers={"content-type": "application/json"},
            ))
            await asyncio.wait_for(writer_sent_body.wait(), 5)
        finally:
            unblock_reader.set()
            result = await read
        assert result.status_code == 200
        assert result.json() == old
        assert (await write).json() == {"ok": True}
    assert json.loads(source.read_text()) == new


def test_skill_import_copies_tree_in_worker(client, monkeypatch, tmp_path):
    source = tmp_path / "skill-source"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: sample-import\ndescription: sample\n---\nBody\n")
    (source / "reference.txt").write_text("keep this too")
    states = _trace_loop(monkeypatch, shutil, "copytree", when=lambda p, *_, **_kw: p == source.resolve())
    response = client.post("/api/skills/import-dir", json={"path": str(source)})
    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "scanned": 1,
        "imported": [{"name": "sample-import", "description": "sample", "from": str(source.resolve())}],
        "skipped": [], "errors": [],
    }
    assert client.get("/api/skills/sample-import/body").json() == {"ok": True, "body": "Body"}
    assert (paths.global_skills_dir() / "sample-import/reference.txt").read_text() == "keep this too"
    assert states == [False]
