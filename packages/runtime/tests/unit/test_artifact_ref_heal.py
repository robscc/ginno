"""Unit: workspace-relative artifact refs — normalization + heal.

Regression for the 2026-08 us_stock_report incident: the model echoes
write_file's relative path into artifact_register/attach_ref. The ref must
be pinned to the session workspace — resolving it against the sidecar cwd
finds nothing, so the panel shows "file missing" and preview/injection break.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime import artifacts as art_store
from ginno_runtime import paths, server
from ginno_runtime.files import get_registry, reset_registries

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_registry(isolated_home):
    reset_registries()
    yield
    reset_registries()


def _ws_dir(sid: str) -> Path:
    d = paths.session_files_dir("default", sid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_session(sid: str, workspace: Path) -> None:
    """In-memory session meta — what create_session leaves behind."""
    server._SESSIONS[sid] = {
        "session_id": sid,
        "project_slug": "default",
        "workspace": str(workspace),
    }


# ---- _normalize_file_ref -------------------------------------------------- #


def test_normalize_relative_ref_against_workspace():
    sid = "s1"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    (ws / "report.html").write_text("<html/>", encoding="utf-8")

    out = server._normalize_file_ref("default", sid, "report.html")
    assert out == str((ws / "report.html").resolve())


def test_normalize_relative_ref_in_subdir():
    sid = "s2"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    (ws / "out").mkdir()
    (ws / "out" / "r.csv").write_text("a,b\n1,2", encoding="utf-8")

    out = server._normalize_file_ref("default", sid, "out/r.csv")
    assert out == str((ws / "out" / "r.csv").resolve())


def test_normalize_missing_file_passes_through():
    sid = "s3"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)

    # Nothing on disk to pin against → keep the ref untouched (never invent
    # a path), same for empty refs.
    assert server._normalize_file_ref("default", sid, "ghost.html") == "ghost.html"
    assert server._normalize_file_ref("default", sid, "") == ""


def test_normalize_absolute_existing_path_kept():
    sid = "s4"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    f = ws / "abs.txt"
    f.write_text("x", encoding="utf-8")

    assert server._normalize_file_ref("default", sid, str(f)) == str(f.resolve())


def test_normalize_falls_back_to_layout_without_meta():
    sid = "s5"
    ws = _ws_dir(sid)  # deliberately no _seed_session — meta lookup misses
    (ws / "report.html").write_text("<html/>", encoding="utf-8")

    out = server._normalize_file_ref("default", sid, "report.html")
    assert out == str((ws / "report.html").resolve())


# ---- _heal_workspace_ref ---------------------------------------------------- #


def test_heal_rewrites_legacy_relative_ref_and_registers():
    sid = "s6"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    (ws / "report.html").write_text("<html/>", encoding="utf-8")

    # Legacy record shape: relative ref, no registry entry.
    art = art_store.add_artifact("default", "file", "美股日报", "report.html", sid)
    new = server._heal_workspace_ref(art, "default")
    assert new == str((ws / "report.html").resolve())

    healed = art_store.get_artifact("default", art["id"])
    assert healed["ref"] == new
    entry = get_registry("default").find_by_path(new)
    assert entry is not None and entry["artifact_id"] == art["id"]


def test_heal_noop_when_file_exists_missing_or_orphan():
    sid = "s7"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    f = ws / "ok.txt"
    f.write_text("x", encoding="utf-8")

    ok = art_store.add_artifact("default", "file", "ok", str(f), sid)
    assert server._heal_workspace_ref(ok, "default") is None  # exists → healthy

    ghost = art_store.add_artifact("default", "file", "ghost", "ghost.html", sid)
    assert server._heal_workspace_ref(ghost, "default") is None  # missing → can't heal

    orphan = art_store.add_artifact("default", "file", "orphan", "o.html", None)
    assert server._heal_workspace_ref(orphan, "default") is None  # no session → can't heal


def test_heal_ignores_broken_absolute_ref():
    sid = "s8"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    (ws / "real.html").write_text("<html/>", encoding="utf-8")

    # A broken absolute ref must not be silently re-pointed elsewhere:
    # joining an absolute path onto the workspace yields the path itself,
    # which is still missing — so nothing heals and the vault healer (the
    # next step in the metadata endpoint) gets its turn.
    art = art_store.add_artifact("default", "file", "gone", "/nonexistent/real.html", sid)
    assert server._heal_workspace_ref(art, "default") is None


# ---- _resolve_attached_files (prompt injection path) ------------------------ #


def test_resolve_attached_files_heals_relative_artifact_ref():
    sid = "s9"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    (ws / "report.html").write_text("<html/>", encoding="utf-8")
    art = art_store.add_artifact("default", "file", "美股日报", "report.html", sid)

    # Attached from a DIFFERENT session — the heal must use the artifact's
    # own session workspace, not the attaching session's.
    items = server._resolve_attached_files([{"artifact_id": art["id"]}], "default", "other")
    assert len(items) == 1
    assert items[0]["path"] == str((ws / "report.html").resolve())
    # the store is healed in place, so later calls don't re-resolve
    assert art_store.get_artifact("default", art["id"])["ref"] == items[0]["path"]


# ---- _tool_file_effects (touch resolution) ---------------------------------- #


async def test_tool_file_effects_touch_resolves_workspace_relative_path():
    sid = "s10"
    ws = _ws_dir(sid)
    _seed_session(sid, ws)
    f = ws / "report.html"
    f.write_text("x", encoding="utf-8")
    entry = get_registry("default").register("report.html", f, session_id=sid)

    sent: list[dict] = []

    async def safe_send(obj):
        sent.append(obj)

    def emit(name, payload):
        return {"event": name, **payload}

    # write_file's path arg is workspace-relative; the touch must resolve it
    # the same way the tool does (not against the sidecar cwd) so the
    # registered entry is matched and previews get invalidated.
    await server._tool_file_effects(
        safe_send, emit, "default", sid, ("write_file", {"path": "report.html"}), "wrote 1 bytes"
    )
    assert any(
        e.get("event") == "preview.invalidate" and e.get("file_id") == entry["id"] for e in sent
    )
