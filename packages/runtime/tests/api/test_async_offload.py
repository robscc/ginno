"""Offload regression tests: heavy synchronous file/archive/JSON work in the
files/export/memory API must run in a worker thread, never on the event loop.

The ``offloop`` probe wraps ``asyncio.to_thread`` and records, per offloaded
callable, whether a running event loop was present at execution time — a
plain worker thread has none. Pre-fix code called the blocking functions
directly from the async endpoint body, so the expected record never appears
and these assertions go red.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


@pytest.fixture
def offloop(monkeypatch):
    real = asyncio.to_thread
    seen: list[tuple[str, bool]] = []

    def probe(func, /, *a, **kw):
        def wrapped(*aa, **kk):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                seen.append((getattr(func, "__name__", str(func)), False))
            else:
                seen.append((getattr(func, "__name__", str(func)), True))
            return func(*aa, **kk)

        return real(wrapped, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", probe)
    return seen


@pytest.fixture
def sid(client, patch_build_model, tmp_path):
    patch_build_model(script(text="ok"))
    ws = tmp_path / "ws"
    ws.mkdir()
    r = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": str(ws), "agent_id": "dev"},
    ).json()
    assert r["ok"] is True
    return r["id"]


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["产品", "金额"])
    ws.append(["A", 10.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, sid, name, body, mime="application/octet-stream"):
    r = client.post(
        "/api/files",
        data={"session_id": sid},
        files={"file": (name, body, mime)},
    ).json()
    assert r["ok"] is True
    return r["file"]


def test_inline_html_off_event_loop(client, sid, offloop):
    html = b"<html><body><p>hi</p></body></html>"
    fid = _upload(client, sid, "p.html", html, "text/html")["id"]
    r = client.get(f"/api/files/{fid}/inline")
    assert r.status_code == 200
    assert r.content == html
    assert r.headers["content-type"].startswith("text/html")
    assert "sandbox" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # read_bytes 必须发生在没有运行中事件循环的工作线程里
    assert ("read_bytes", False) in offloop


def test_download_csv_export_off_event_loop(client, sid, offloop):
    fid = _upload(client, sid, "报表.xlsx", _xlsx_bytes())["id"]
    r = client.get(f"/api/files/{fid}/download?fmt=csv&sheet=销售")
    assert r.status_code == 200
    assert "产品,金额" in r.content.decode("utf-8-sig")
    assert ("build_csv_export", False) in offloop


def test_save_to_downloads_off_event_loop(client, sid, offloop, monkeypatch, tmp_path):
    monkeypatch.setenv("GINNO_DOWNLOADS", str(tmp_path / "dl"))
    fid = _upload(client, sid, "t.csv", b"a,b\n1,2\n", "text/csv")["id"]
    r = client.post(f"/api/files/{fid}/save-to-downloads", json={"fmt": "raw"}).json()
    assert r["ok"] is True and (tmp_path / "dl" / "t.csv").is_file()
    assert ("_export", False) in offloop


def test_session_file_dirs_off_event_loop(client, sid, offloop):
    r = client.get("/api/session-files/dirs")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ("_collect", False) in offloop


def test_export_session_markdown_off_event_loop(client, sid, offloop):
    r = client.get(f"/api/export/session/{sid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert ("_render_markdown", False) in offloop


def test_export_project_zip_off_event_loop(client, sid, offloop):
    r = client.post("/api/export/project", json={"project_slug": "default"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert ("_build_zip", False) in offloop


def test_get_memory_off_event_loop(client, offloop):
    r = client.get("/api/memory")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ("_collect", False) in offloop
