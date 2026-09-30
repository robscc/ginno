"""Image preview for the code panel: classification + ``/api/code/raw``.

Two things are guarded here.

**Classification** — ``code_read`` must flag an image with
``readonly_reason="image"`` (not ``"binary"``) so the frontend routes it to
``ImagePreview`` instead of rendering the "cannot open as text" notice. The set
of extensions that count as an image comes from
``files.extractors.IMAGE_EXTS``, the repo's single definition of "image".

**Transport** — ``/api/code/raw`` streams the bytes for ``<img src>``. It is the
one endpoint whose body is not JSON, because a ``{ok:false}`` body would render
as a corrupt picture rather than as a failure; so failures are real HTTP
statuses and the machine-readable code rides in ``X-Ginno-Code``. It walks the
SAME fence as ``read`` — a path the panel may not read is a path it may not
stream — which is asserted here too, since a second endpoint is exactly where a
fence gets forgotten.

Session resolution (cold vs warm) is covered by ``test_code_fence.py``; these
tests use a warm session so they can stay focused on the raw/image behaviour.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import paths
from ginno_runtime.api import code as code_panel
from ginno_runtime.server_shared import _SESSIONS

pytestmark = pytest.mark.unit

SLUG = "default"
SID = "s-raw"
Q = {"project_slug": SLUG, "session_id": SID}


def _png(w: int = 3, h: int = 2) -> bytes:
    """A genuinely valid PNG.

    It must be REAL bytes, not filler: ``code_read`` classifies a file as binary
    by scanning for a NUL in the first ``BINARY_SNIFF_BYTES``, and only a real
    PNG's chunk framing (a big-endian length field of 13 in IHDR) guarantees
    those. Fake bytes ending in ``.png`` would take the text path and the test
    would be asserting nothing.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def client(tmp_path):
    """A warm session with the code router mounted, plus its workspace dir.

    Warm (``_SESSIONS``) rather than seeded on disk: the raw endpoint resolves
    the session exactly like ``read`` does, and that resolution is already
    tested against cold sessions elsewhere. The entry is removed afterwards so
    the module-level dict does not leak into other tests.
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


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_read_flags_a_png_as_image_not_binary(client):
    """`image` is the one readonly_reason that is not a degradation: the panel
    can show the file, just not as text."""
    c, ws = client
    (ws / "shot.png").write_bytes(_png())

    r = c.get("/api/code/read", params={**Q, "root": "session", "path": "shot.png"}).json()

    assert r["ok"] is True
    assert r["readonly_reason"] == "image"
    assert r["text"] is None
    assert r["editable"] is False


def test_read_keeps_non_image_binary_as_binary(client):
    """A non-image binary must keep the plain `binary` reason, or the frontend
    would try to render a picture that does not exist."""
    c, ws = client
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02NOT AN IMAGE\x00")

    r = c.get("/api/code/read", params={**Q, "root": "session", "path": "blob.bin"}).json()

    assert r["readonly_reason"] == "binary"
    assert r["text"] is None


def test_read_flags_svg_as_image(client):
    """A text-formatted image is still an image.

    SVG's bytes are text, so the binary sniff ALONE would classify it as source
    and the editor would show markup. The image check deliberately runs before
    the sniff so this lands as a picture — this test is what pins that order
    (swap the two blocks in ``code_read`` and this fails).
    """
    c, ws = client
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    (ws / "pic.svg").write_bytes(svg)

    r = c.get("/api/code/read", params={**Q, "root": "session", "path": "pic.svg"}).json()

    assert r["readonly_reason"] == "image"
    assert r["text"] is None
    assert r["editable"] is False


def test_raw_serves_svg_with_a_renderable_type(client):
    """The transport half of the same choice: the bytes must reach the ``<img>``
    with a type the browser will actually draw.

    ``image/svg+xml`` inside an ``<img>`` is inert — the SVG cannot reach this
    origin's DOM or run script — which is why this needs no sandboxing. We
    deliberately offer no inline/iframe surface (design §9.3).
    """
    c, ws = client
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    (ws / "pic.svg").write_bytes(svg)

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": "pic.svg"})

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.content == svg
    assert r.headers["x-content-type-options"] == "nosniff"


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def test_raw_serves_the_exact_bytes_as_an_image(client):
    c, ws = client
    png = _png()
    (ws / "shot.png").write_bytes(png)

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": "shot.png"})

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    # Byte-identical: a preview that re-encodes is a preview that lies.
    assert r.content == png
    # Without nosniff a mis-typed response in a workspace could be interpreted
    # as markup by the webview.
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in r.headers["cache-control"]


@pytest.mark.parametrize(
    "name,payload",
    [
        ("blob.bin", b"\x00\x01\x02BINARY\x00"),
        # Extensions outside the image whitelist, including one the repo's
        # IMAGE_EXTS does not list (ico) — the fallback is by whitelist, not by
        # "looks like binary".
        ("doc.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"),
    ],
)
def test_raw_falls_back_to_octet_stream_for_unlisted_types(client, name, payload):
    """Anything outside the mime whitelist is served as a download, never
    inline — an octet-stream can't be talked into being markup."""
    c, ws = client
    (ws / name).write_bytes(payload)

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": name})

    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment"
    assert r.content == payload


def test_raw_refuses_a_file_over_the_cap(client):
    """The cap is checked from stat() BEFORE reading, so a huge file is never
    pulled into memory just to be rejected."""
    c, ws = client
    (ws / "huge.png").write_bytes(_png() + b"\x00" * (code_panel.RAW_MAX_BYTES + 1))

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": "huge.png"})

    assert r.status_code == 413
    assert r.headers["x-ginno-code"] == "too-large"


# --------------------------------------------------------------------------- #
# The fence is SHARED with `read` — the reason raw is not a separate hole.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["../../../../etc/passwd", "/etc/passwd", "a/../../../etc/passwd"])
def test_raw_refuses_to_leave_the_root(client, bad):
    c, _ws = client

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": bad})

    assert r.status_code == 403
    assert r.headers["x-ginno-code"] == "outside-root"


def test_raw_reports_an_unknown_session_as_a_real_404(client):
    """`<img src>` cannot read a JSON body, so an unknown session must be a real
    status — a 200 carrying {ok:false} would look like a broken picture."""
    c, _ws = client

    r = c.get(
        "/api/code/raw",
        params={"project_slug": SLUG, "session_id": "ghost", "root": "session", "path": "a.png"},
    )

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "unknown-root"


def test_raw_reports_an_unknown_root(client):
    c, ws = client
    (ws / "a.png").write_bytes(_png())

    r = c.get("/api/code/raw", params={**Q, "root": "f_nope", "path": "a.png"})

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "unknown-root"


def test_raw_reports_a_directory_as_not_a_file(client):
    c, ws = client
    (ws / "sub").mkdir(exist_ok=True)

    r = c.get("/api/code/raw", params={**Q, "root": "session", "path": "sub"})

    assert r.status_code == 404
    assert r.headers["x-ginno-code"] == "not-text"


def test_raw_and_read_agree_on_what_is_reachable(client):
    """The two endpoints must not drift: every path raw refuses, read refuses
    too (and vice versa for the session root). Cheap sentinel against someone
    later forking the resolution logic for raw."""
    c, ws = client
    (ws / "ok.png").write_bytes(_png())

    for path in ["ok.png", "../../../etc/passwd", "/etc/passwd"]:
        rr = c.get("/api/code/raw", params={**Q, "root": "session", "path": path})
        rd = c.get("/api/code/read", params={**Q, "root": "session", "path": path})
        assert (rr.status_code == 200) == (rd.json().get("ok") is True), path