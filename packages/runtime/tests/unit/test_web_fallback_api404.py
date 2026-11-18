"""NEW-01 regression: unknown ``/api/*`` GET paths must 404 as JSON.

The static catch-all (``server._serve_web``) used to swallow ANY unknown
``/api/*`` path with 200 + ``index.html`` (app-iter-harness run-20261118-*
findings ×4: 2 medium boundary + 2 low non-JSON), so a frontend fetch got
HTML where it expected JSON — the parse error masked the real 404 — and
boundary probes couldn't distinguish broken endpoints from real pages.
Drive the handler in isolation (import cost is the same module other unit
tests already import).
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "api/__nope__",
        "api/ws/__nope__",
        "api/deep/nested/__nope__",
        "api",  # bare prefix, no trailing slash
    ],
)
async def test_unknown_api_path_is_404_json(path):
    import ginno_runtime.server as server_mod

    resp = await server_mod._serve_web(path)
    assert resp.status_code == 404, f"/{path} must not fall into the SPA fallback"
    body = json.loads(resp.body)
    assert body.get("ok") is False
    assert "error" in body
    assert path in body["error"]


@pytest.mark.asyncio
async def test_non_api_unknown_still_spa_fallback(monkeypatch, tmp_path):
    """Non-API unknown paths keep the SPA fallback (client-side routing)."""
    import ginno_runtime.server as server_mod

    # WEB_OUT may be None in a bare checkout (no apps/web/out) — point it at a
    # scratch dir with an index.html so the fallback path is exercised.
    (tmp_path / "index.html").write_text("<html>ok</html>")
    monkeypatch.setattr(server_mod, "WEB_OUT", tmp_path)
    resp = await server_mod._serve_web("some/client/route")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_guard_ignores_lookalike_prefixes():
    """Only the true ``api`` prefix is guarded — a page route that merely
    starts with the letters "api" (e.g. ``apidemo``) still serves the SPA."""
    import ginno_runtime.server as server_mod

    resp = await server_mod._serve_web("apidemo")
    assert resp.status_code != 404 or "unknown API endpoint" not in (
        resp.body.decode() if resp.body else ""
    )
