"""HTML preview for the code panel: ``/api/code/file/...``.

The transport half of the rendered-page preview. ``raw`` serves images for
``<img src>``, where relative resolution never matters; THIS endpoint exists
because a previewed HTML page's sub-resources (``./style.css``, ``js/app.js``)
resolve against the document URL's PATH — a query string is dropped — so the
route has to carry slug/session/root/path as segments.

Two things are guarded here, mirroring ``test_code_raw.py``:

**Sandbox** — every response carries a CSP ``sandbox allow-scripts`` +
``connect-src 'none'``. This is the header that makes the preview safe: the
app's CORS is ``allow_origins=["*"]``, so without it a previewed page could
fetch the API from its sandboxed origin and read the responses. The Host
header is reflected into ``default-src`` and therefore validated first.

**Fence** — the endpoint walks the SAME ``resolve_code_target`` gates as
``read``/``raw``; a second file-serving endpoint is exactly where a fence gets
forgotten, so the agreement sentinel is repeated here too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-file"


@pytest.fixture
def client(tmp_path):
    """A warm session with the code router mounted, plus its workspace dir.

    Same shape as ``test_code_raw``'s fixture: warm (``_SESSIONS``) session,
    popped again on teardown so the module-level dict does not leak.
    """
    ws = paths.session_files_dir(SLUG, SID)
    ws.mkdir(parents=True, exist_ok=True)
    _SESSIONS[SID] = {"project_slug": SLUG}
    app = FastAPI()
    app.include_router(code_panel.router)
    try:
        yield TestClient(app), ws
    finally:
        _SESSIONS.pop(SID, None)


def _page(extra: str = "") -> bytes:
    return f"<!doctype html><html><body><h1>hi</h1>{extra}</body></html>".encode()


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def test_file_serves_html_with_a_document_type(client):
    c, ws = client
    (ws / "index.html").write_bytes(_page())

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/index.html")

    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.content == _page()
    assert r.headers["x-content-type-options"] == "nosniff"


def test_file_serves_a_nested_subresource_at_its_path(client):
    """The reason this route is path-shaped: ``./style.css`` from
    ``/dir/index.html`` resolves to ``/dir/style.css`` and must land here."""
    c, ws = client
    (ws / "dir").mkdir()
    (ws / "dir" / "index.html").write_bytes(_page(b'<link rel="stylesheet" href="style.css">'))
    (ws / "dir" / "style.css").write_bytes(b"body{color:red}")

    page = c.get(f"/api/code/file/{SLUG}/{SID}/session/dir/index.html")
    css = c.get(f"/api/code/file/{SLUG}/{SID}/session/dir/style.css")

    assert page.status_code == 200
    assert css.status_code == 200
    assert css.headers["content-type"] == "text/css; charset=utf-8"
    assert css.content == b"body{color:red}"


def test_file_never_caches_by_default(client):
    """Sub-resources carry no cache-busting version (relative resolution drops
    the query), so revalidation is the only thing keeping a rewritten page
    from mixing old assets with new markup."""
    c, ws = client
    (ws / "a.js").write_bytes(b"console.log(1)")

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/a.js")

    assert r.headers["cache-control"] == "no-cache"


def test_file_falls_back_to_attachment_for_unlisted_types(client):
    c, ws = client
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4\n")

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/doc.pdf")

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment"


def test_file_refuses_a_file_over_the_cap(client):
    c, ws = client
    (ws / "huge.html").write_bytes(_page() + b"\x00" * (code_panel.RAW_MAX_BYTES + 1))

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/huge.html")

    assert r.status_code == 413
    assert r.headers["x-ginno-code"] == "too-large"


# --------------------------------------------------------------------------- #
# Sandbox headers — the reason this endpoint is safe to exist at all
# --------------------------------------------------------------------------- #
def test_file_sandboxes_every_document_response(client):
    """CSP sandbox + connect-src 'none' on every response. CORS below is
    allow_origins=["*"], so this header is what stops a previewed page from
    calling the API and reading the responses."""
    c, ws = client
    (ws / "index.html").write_bytes(_page())

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/index.html")

    csp = r.headers["content-security-policy"]
    assert "sandbox allow-scripts" in csp
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
    # allow-same-origin must never appear — that would undo the opaque origin.
    assert "allow-same-origin" not in csp


def test_file_reflects_only_a_wellformed_host_into_the_csp(client):
    """The Host header lands in a CSP source list, so it is validated before
    reflection; garbage degrades to 'none' (inert page) instead of trusting
    the request."""
    c, ws = client
    (ws / "index.html").write_bytes(_page())
    url = f"/api/code/file/{SLUG}/{SID}/session/index.html"

    ok = c.get(url, headers={"Host": "127.0.0.1:8787"})
    evil = c.get(url, headers={"Host": "evil.example/x '\\; script-src 'none'"})

    assert "http://127.0.0.1:8787" in ok.headers["content-security-policy"]
    assert "default-src 'none'" in evil.headers["content-security-policy"]


# --------------------------------------------------------------------------- #
# The fence is SHARED with read/raw
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad",
    [
        # %2E%2E, not literal dots: a client (httpx) normalizes dot segments in
        # the URL before the server ever sees them, while a browser forwards
        # percent-encoded ones verbatim — which is the traversal that matters.
        "%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
        "/etc/passwd",
        "a/%2e%2e/%2e%2e/etc/passwd",
    ],
)
def test_file_refuses_to_leave_the_root(client, bad):
    c, _ws = client

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/{bad}")

    assert r.status_code == 403
    assert r.headers["x-ginno-code"] == "outside-root"


def test_file_reports_an_unknown_session_as_a_real_404(client):
    c, _ws = client

    r = c.get(f"/api/code/file/{SLUG}/ghost/session/a.html")

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "unknown-root"


def test_file_reports_an_unknown_root(client):
    c, ws = client
    (ws / "a.html").write_bytes(_page())

    r = c.get(f"/api/code/file/{SLUG}/{SID}/f_nope/a.html")

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "unknown-root"


def test_file_reports_a_directory_as_not_a_file(client):
    c, ws = client
    (ws / "sub").mkdir()

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/sub")

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "not-text"


def test_file_and_read_agree_on_what_is_reachable(client):
    """No drift between the endpoints: every path the preview file server
    refuses, read refuses too. Same sentinel as test_code_raw."""
    c, ws = client
    (ws / "ok.html").write_bytes(_page())

    for path in ["ok.html", "../../../etc/passwd", "/etc/passwd"]:
        rf = c.get(f"/api/code/file/{SLUG}/{SID}/session/{path}")
        rd = c.get("/api/code/read", params={
            "project_slug": SLUG, "session_id": SID, "root": "session", "path": path,
        })
        assert (rf.status_code == 200) == (rd.json().get("ok") is True), path


def test_file_rejects_a_session_workspace_symlink_escaping_the_root(client, tmp_path: Path):
    """Gate 3 dereferences symlinks; the path-shaped route must not reintroduce
    a lexical bypass just because the path comes from URL segments."""
    c, ws = client
    outside = tmp_path / "outside.html"
    outside.write_bytes(_page())
    (ws / "link.html").symlink_to(outside)

    r = c.get(f"/api/code/file/{SLUG}/{SID}/session/link.html")

    assert r.status_code == 403
    assert r.headers["x-ginno-code"] == "outside-root"
