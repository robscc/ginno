"""E2E smoke: the P2 stop endpoint is registered and answers 202 on an idle
session (P2 shared contract 4)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_stop_endpoint_route_exists(client, create_session):
    from ginno_runtime.testing.fake_model import script

    sid = create_session([script(text="hi")])
    r = client.post(f"/api/sessions/{sid}/stop")
    assert r.status_code == 202
    assert r.json()["stopped"] is True
