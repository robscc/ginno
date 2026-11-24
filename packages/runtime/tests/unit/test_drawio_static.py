"""Serve both forms of the local draw.io viewer through its normal URL."""

import gzip

import httpx
import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(("packaged", "accept_encoding", "encoded"), [
    (False, "gzip", False),
    (True, "gzip, deflate, br", True),
    (True, "GZIP;q=0.5", True),
    (True, "*;q=0.5", True),
    (True, "Gzip;q=0, *;q=1", False),
    (True, "identity", False),
    (True, "", False),
])
async def test_drawio_viewer_representation(tmp_path, monkeypatch, packaged, accept_encoding, encoded):
    from ginno_runtime import server

    (tmp_path / "index.html").write_text("<html>SPA fallback</html>")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    script = b"window.drawio = true;\n"
    compressed = gzip.compress(script, mtime=0)
    if packaged:
        (vendor / "viewer-static.min.js.gz").write_bytes(compressed)
    else:
        (vendor / "viewer-static.min.js").write_bytes(script)
    monkeypatch.setattr(server, "WEB_OUT", tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get(
            "/vendor/viewer-static.min.js", headers={"Accept-Encoding": accept_encoding}
        )

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.content == script
    assert response.headers.get("content-encoding") == ("gzip" if encoded else None)
    assert int(response.headers["content-length"]) == len(compressed if encoded else script)
    if packaged:
        assert response.headers["vary"] == "Accept-Encoding"
