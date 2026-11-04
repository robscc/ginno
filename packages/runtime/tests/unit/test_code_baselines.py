"""Session baselines + ``GET /api/code/diff`` (code-panel live diff, design §6.1/§6.2).

Pins the runtime-side data contract the frontend relies on:

* **first-write-wins** — the baseline is the state BEFORE the agent's first
  write; later writes must never overwrite it (that is what makes "diff against
  what I had" work across a long turn).
* **session isolation + cleanup** — one session's baseline is invisible to
  another, and ``clear_session`` (wired into the session-delete hook) drops it.
* **caps degrade, they do not fail** — an over-2 MB file records only the
  version token with ``degraded=True``; a non-existent file records an empty
  baseline (an added file).
* **event field** — ``code.changed`` gains ``base_version`` (null when no
  baseline); every pre-existing field keeps its exact value.
* **endpoint shape** — base/current content + versions for a real session,
  fence errors reused, and a non-git root answers normally (design S5).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.files import baselines
from ginno_runtime.server_shared import _SESSIONS
from ginno_runtime.tools.builtin import build_builtin_tools

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-baseline"
Q = {"project_slug": SLUG, "session_id": SID}


@pytest.fixture(autouse=True)
def _clean_maps():
    baselines._BASELINES.clear()
    yield
    baselines._BASELINES.clear()


@pytest.fixture
def ws(tmp_path_factory):
    """A workspace OUTSIDE $GINNO_HOME (isolated_home sets home=tmp_path,
    so using tmp_path itself would trip _path_denied)."""
    return tmp_path_factory.mktemp("ws")


def _real(ws: Path) -> Path:
    """macOS ``/var`` → ``/private/var``: the baseline map keys on realpath."""
    import os
    return Path(os.path.realpath(str(ws)))


def _tools(ws: Path, session_id: str | None = SID):
    return {t.name: t for t in build_builtin_tools(str(_real(ws)), session_id=session_id)}


# --------------------------------------------------------------------------- #
# baseline store
# --------------------------------------------------------------------------- #
def test_first_write_only_snapshot(ws):
    """The original bytes survive every later write."""
    f = ws / "a.py"
    f.write_text("v0\n", encoding="utf-8")
    tools = _tools(ws)
    tools["write_file"].invoke({"path": "a.py", "content": "v1\n"})
    b = baselines.get_baseline(SID, _real(f))
    assert b["content"] == "v0\n"
    assert b["base_version"] == "" or ":" in b["base_version"]

    tools["edit_file"].invoke({"path": "a.py", "old": "v1", "new": "v2"})
    tools["write_file"].invoke({"path": "a.py", "content": "v3\n"})
    assert baselines.get_baseline(SID, _real(f))["content"] == "v0\n"
    assert f.read_text(encoding="utf-8") == "v3\n"


def test_created_file_has_empty_baseline(ws):
    """A file the agent creates has no pre-content — the frontend treats the
    whole file as added."""
    tools = _tools(ws)
    tools["write_file"].invoke({"path": "new.txt", "content": "hello\n"})
    b = baselines.get_baseline(SID, _real(ws / "new.txt"))
    assert b == {"content": None, "base_version": "", "degraded": False}


def test_oversize_file_degrades_not_fails(ws, monkeypatch):
    f = ws / "big.txt"
    f.write_bytes(b"x" * 100)
    monkeypatch.setattr(baselines, "MAX_FILE_BYTES", 50)
    tools = _tools(ws)
    tools["write_file"].invoke({"path": "big.txt", "content": "small\n"})
    b = baselines.get_baseline(SID, _real(f))
    assert b["degraded"] is True
    assert b["content"] is None
    assert ":" in b["base_version"]  # version token still recorded


def test_per_session_cap_marks_degraded(ws, monkeypatch):
    monkeypatch.setattr(baselines, "PER_SESSION_MAX_FILES", 1)
    old = ws / "a.txt"
    old.write_text("A\n", encoding="utf-8")
    b = ws / "b.txt"
    b.write_text("B\n", encoding="utf-8")
    e = baselines.snapshot_before_write(SID, old)
    assert e["content"] == "A\n" and e["degraded"] is False
    e2 = baselines.snapshot_before_write(SID, b)
    assert e2["degraded"] is True and e2["content"] is None


def test_sessions_are_isolated_and_cleared(ws):
    f = ws / "x.txt"
    f.write_text("orig\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, _real(f))
    baselines.snapshot_before_write("other-session", _real(f))
    assert baselines.get_baseline("other-session", _real(f))["content"] == "orig\n"
    baselines.clear_session(SID)
    assert baselines.get_baseline(SID, _real(f)) is None
    assert baselines.get_baseline("other-session", _real(f)) is not None


def test_no_session_id_skips_snapshot(ws):
    """Workflow engine / tests calling the tools without a session: no crash,
    no baseline."""
    f = ws / "y.txt"
    f.write_text("orig\n", encoding="utf-8")
    tools = _tools(ws, session_id=None)
    out = tools["write_file"].invoke({"path": "y.txt", "content": "new\n"})
    assert "wrote" in out
    assert baselines._BASELINES == {}


# --------------------------------------------------------------------------- #
# code.changed event decoration
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_code_changed_event_carries_base_version(ws):
    from ginno_runtime.api.stream.effects import _emit_code_changes
    from ginno_runtime.files.code_changes import encode_code_marker

    f = ws / "z.py"
    f.write_text("before\n", encoding="utf-8")
    entry = baselines.snapshot_before_write(SID, _real(f))
    marker = encode_code_marker(
        [{"path": str(f), "op": "edit", "version": "9:123"}]
    )

    sent: list[tuple[str, dict]] = []

    async def safe_send(evt):
        sent.append((evt["type"], evt["payload"]))

    def emit(kind, payload):
        return {"type": kind, "payload": payload}

    await _emit_code_changes(safe_send, emit, SLUG, SID, "ok" + marker)
    assert len(sent) == 1
    kind, payload = sent[0]
    assert kind == "code.changed"
    # old fields untouched
    assert payload["session_id"] == SID
    assert payload["path"] == str(f)
    assert payload["op"] == "edit"
    assert payload["version"] == "9:123"
    # new fields
    assert payload["base_version"] == entry["base_version"]
    # turn_id: passed through when provided (P1 2026-11-04), null when not.
    assert payload["turn_id"] is None


@pytest.mark.asyncio
async def test_code_changed_event_null_base_version_without_baseline(ws):
    from ginno_runtime.api.stream.effects import _emit_code_changes
    from ginno_runtime.files.code_changes import encode_code_marker

    f = ws / "never-agent-written.py"
    f.write_text("x\n", encoding="utf-8")
    sent: list[dict] = []

    async def safe_send(evt):
        sent.append(evt["payload"])

    await _emit_code_changes(
        safe_send, lambda k, p: {"type": k, "payload": p}, SLUG, SID,
        encode_code_marker([{"path": str(f), "op": "write", "version": "2:1"}]),
    )
    assert sent[0]["base_version"] is None


# --------------------------------------------------------------------------- #
# GET /api/code/diff
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path_factory):
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    mount = tmp_path_factory.mktemp("mount")  # deliberately NOT a git repo
    _SESSIONS[SID] = {
        "project_slug": SLUG,
        "context_dirs": [
            {"id": "f_m", "path": str(mount), "name": "m", "access": "rw"}
        ],
        "primary_path": str(mount),
    }
    app = FastAPI()
    app.include_router(code_panel.router)
    try:
        yield TestClient(app), ws, mount
    finally:
        _SESSIONS.pop(SID, None)


def _get_diff(c, root, path, **kw):
    params = {**Q, "root": root, "path": path, **kw}
    return c.get("/api/code/diff", params=params)


def test_diff_endpoint_returns_base_and_current(client):
    c, _ws, mount = client
    f = mount / "svc.py"
    f.write_text("def a():\n    return 1\n", encoding="utf-8")
    tools = _tools(mount)
    tools["edit_file"].invoke({"path": "svc.py", "old": "return 1", "new": "return 2"})

    r = _get_diff(c, "f_m", "svc.py")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["baseline_exists"] is True
    assert body["base_content"] == "def a():\n    return 1\n"
    assert body["current_content"] == "def a():\n    return 2\n"
    assert body["degraded"] is False
    # current_version uses the same `size:mtime_ns` identity as read/write
    st = f.stat()
    assert body["current_version"] == f"{st.st_size}:{st.st_mtime_ns}"
    assert body["base_version"] != body["current_version"]


def test_diff_endpoint_unknown_baseline(client):
    c, _ws, mount = client
    f = mount / "untouched.py"
    f.write_text("hi\n", encoding="utf-8")
    body = _get_diff(c, "f_m", "untouched.py").json()
    assert body["ok"] is True and body["baseline_exists"] is False
    assert body["base_content"] is None and body["base_version"] == ""


def test_diff_endpoint_degraded_when_file_deleted(client):
    c, _ws, mount = client
    f = mount / "gone.py"
    f.write_text("was here\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, _real(f))
    f.unlink()
    body = _get_diff(c, "f_m", "gone.py").json()
    assert body["ok"] is True
    assert body["base_content"] == "was here\n"
    assert body["current_content"] is None
    assert body["current_version"] == ""


def test_diff_fence_errors_are_json_not_500(client):
    c, _ws, _mount = client
    r = _get_diff(c, "nope", "x.txt")
    assert r.status_code == 200
    assert r.json()["code"] == "unknown-root"
    r = _get_diff(c, "f_m", "../escape.txt")
    assert r.json()["code"] == "outside-root"
    r = c.get("/api/code/diff", params={"project_slug": SLUG, "session_id": "", "root": "f_m", "path": "x"})
    assert r.status_code == 400


def test_diff_non_git_root_works(client):
    """Design S5: the session workspace root is not a git repo either — the
    endpoint must answer exactly the same way."""
    c, ws, _mount = client
    f = ws / "notes.md"
    f.write_text("# old\n", encoding="utf-8")
    baselines.snapshot_before_write(SID, _real(f))
    f.write_text("# new\n", encoding="utf-8")
    body = _get_diff(c, "session", "notes.md").json()
    assert body["ok"] is True
    assert body["base_content"] == "# old\n"
    assert body["current_content"] == "# new\n"


@pytest.mark.asyncio
async def test_code_changed_event_carries_turn_id(ws):
    from ginno_runtime.api.stream.effects import _emit_code_changes
    from ginno_runtime.files.code_changes import encode_code_marker

    f = ws / "z2.py"
    f.write_text("x\n", encoding="utf-8")
    marker = encode_code_marker([{"path": str(f), "op": "write", "version": "1:2"}])
    sent: list[tuple[str, dict]] = []

    async def safe_send(evt):
        sent.append((evt["type"], evt["payload"]))

    def emit(kind, payload):
        return {"type": kind, "payload": payload}

    await _emit_code_changes(safe_send, emit, SLUG, SID, "ok" + marker, turn_id="t-42")
    assert sent and sent[0][1]["turn_id"] == "t-42"
