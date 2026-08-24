"""Ginno personal agent runtime."""

import os

from ._version import __version__

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _ensure_loopback_no_proxy() -> None:
    """Seed ``no_proxy``/``NO_PROXY`` with loopback hosts (idempotent,
    merge-preserving).

    httpx (the transport under the anthropic/openai SDKs) honours the macOS
    system proxy via ``urllib.request.getproxies()`` when ``trust_env=True``
    but — unlike curl — ignores the system exception list. A provider
    ``base_url`` pointing at a local proxy (e.g. 127.0.0.1:15721) would then
    be routed THROUGH the system proxy and come back as an empty
    ``InternalServerError: Error code: 502``. Loopback destinations never
    need an upstream proxy, so bypass unconditionally.
    (2026-08-19: settings verify + every turn 502'd while Clash was the
    system proxy.)
    """
    for key in ("no_proxy", "NO_PROXY"):
        cur = os.environ.get(key, "")
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        missing = [h for h in _LOOPBACK_HOSTS if h not in parts]
        if missing:
            os.environ[key] = ",".join(parts + missing)


_ensure_loopback_no_proxy()

__all__ = ["__version__"]
