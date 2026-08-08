"""E2E: file artifacts registered with workspace-relative refs.

Models echo the relative path they passed to write_file into
artifact_register / attach_ref. The WS layer must pin such refs to the
session workspace (and register the file) so exists checks, previews and
prompt injection all resolve — regression for the 2026-08 us_stock_report
incident where the panel showed "磁盘上找不到该文件" while the file sat in
sessions/<sid>/ all along.
"""

from __future__ import annotations

import pytest
from conftest import events_of

from ginno_runtime import artifacts as art_store
from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script, script_tool_call

pytestmark = pytest.mark.e2e


def _ws_file(sid: str, name: str, content: str = "<html>hi</html>") -> str:
    """Write a file into the session workspace; return its resolved path."""
    ws = paths.session_files_dir("default", sid)
    f = ws / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    return str(f.resolve())


def test_artifact_register_relative_ref_is_normalized(create_session, ws_conv, client):
    model = [
        script(
            tool_calls=[
                script_tool_call(
                    "artifact_register",
                    {"kind": "file", "name": "美股日报", "ref": "report.html"},
                )
            ]
        ),
        script(text="registered"),
    ]
    sid = create_session(model)
    abs_path = _ws_file(sid, "report.html")

    with ws_conv(sid) as conv:
        conv.invoke("register the report")
        events = conv.recv_until("message.end", "error")
    assert not events_of(events, "error")

    arts = client.get("/api/artifacts?project_slug=default").json()
    art = next(a for a in arts if a["name"] == "美股日报")
    assert art["ref"] == abs_path  # absolute, not the raw relative echo

    meta = client.get(f"/api/artifacts/{art['id']}/metadata?project_slug=default").json()
    assert meta["exists"] is True
    assert meta["file"] is not None and meta["file"]["path"] == abs_path

    # preview/download depend on a files-registry entry — must exist now
    files = client.get("/api/files?project_slug=default").json()
    assert any(f["path"] == abs_path for f in files)


def test_attach_ref_relative_ref_id_is_normalized(create_session, ws_conv, client):
    model = [
        script(
            tool_calls=[
                script_tool_call(
                    "attach_ref",
                    {"kind": "file", "name": "report.html", "ref_id": "report.html"},
                )
            ]
        ),
        script(text="attached"),
    ]
    sid = create_session(model)
    abs_path = _ws_file(sid, "report.html")

    with ws_conv(sid) as conv:
        conv.invoke("attach the report")
        events = conv.recv_until("message.end", "error")

    refs = events_of(events, "ref.emit")
    assert len(refs) == 1
    assert refs[0]["ref_id"] == abs_path  # chip already carries the healed path

    arts = client.get("/api/artifacts?project_slug=default").json()
    art = next(a for a in arts if a["name"] == "report.html")
    assert art["ref"] == abs_path


def test_artifact_register_missing_file_ref_passes_through(create_session, ws_conv, client):
    model = [
        script(
            tool_calls=[
                script_tool_call(
                    "artifact_register",
                    {"kind": "file", "name": "Ghost", "ref": "ghost.html"},
                )
            ]
        ),
        script(text="registered"),
    ]
    sid = create_session(model)  # no ghost.html written anywhere

    with ws_conv(sid) as conv:
        conv.invoke("register")
        events = conv.recv_until("message.end", "error")
    assert not events_of(events, "error")

    art = next(
        a for a in client.get("/api/artifacts?project_slug=default").json() if a["name"] == "Ghost"
    )
    assert art["ref"] == "ghost.html"  # untouched — nothing to pin against
    meta = client.get(f"/api/artifacts/{art['id']}/metadata?project_slug=default").json()
    assert meta["exists"] is False


def test_metadata_heals_legacy_relative_ref(create_session, client):
    """Pre-fix records (relative ref, no registry entry) heal on first
    metadata read instead of showing "file missing" forever."""
    sid = create_session([script(text="hi")])
    abs_path = _ws_file(sid, "legacy.html", "<html>legacy</html>")

    art = art_store.add_artifact("default", "file", "Legacy Report", "legacy.html", sid)
    assert art["ref"] == "legacy.html"

    meta = client.get(f"/api/artifacts/{art['id']}/metadata?project_slug=default").json()
    assert meta["exists"] is True
    assert meta["file"] is not None and meta["file"]["path"] == abs_path

    healed = next(
        a
        for a in client.get("/api/artifacts?project_slug=default").json()
        if a["id"] == art["id"]
    )
    assert healed["ref"] == abs_path  # store rewritten in place

    files = client.get("/api/files?project_slug=default").json()
    assert any(f["path"] == abs_path for f in files)


def test_non_file_kind_refs_are_never_path_mangled(create_session, ws_conv, client):
    model = [
        script(
            tool_calls=[
                script_tool_call(
                    "artifact_register",
                    {"kind": "link", "name": "GitHub", "ref": "https://github.com"},
                )
            ]
        ),
        script(text="registered"),
    ]
    sid = create_session(model)

    with ws_conv(sid) as conv:
        conv.invoke("register the link")
        events = conv.recv_until("message.end", "error")
    assert not events_of(events, "error")

    art = next(
        a for a in client.get("/api/artifacts?project_slug=default").json() if a["name"] == "GitHub"
    )
    assert art["ref"] == "https://github.com"
