"""High-risk domain policy (design §11.2).

Payment / bank / weixin hosts force an ask even when bypass_permissions is on.
Rules live in ``settings.browser.risky_domains`` so the user can edit them.
"""

from __future__ import annotations

import fnmatch
import json
import re
from urllib.parse import urlparse

from .. import paths

DEFAULT_RISKY_DOMAINS = [
    "*://*.bank*",
    "*://*.alipay*",
    "*://*.weixin*",
    "*://*.tenpay*",
    "*://*.paypal*",
]

# Design §13: 改密 paths force a handoff even on an otherwise-allow host.
_RISKY_PATH_HINTS = (
    "/change-password",
    "/password/change",
    "/account/password",
    "/settings/security/password",
)


def load_risky_domains() -> list[str]:
    p = paths.settings_path()
    if not p.exists():
        return list(DEFAULT_RISKY_DOMAINS)
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return list(DEFAULT_RISKY_DOMAINS)
    browser = data.get("browser") if isinstance(data, dict) else None
    if not isinstance(browser, dict):
        return list(DEFAULT_RISKY_DOMAINS)
    raw = browser.get("risky_domains")
    if raw is None:
        return list(DEFAULT_RISKY_DOMAINS)
    if not isinstance(raw, list):
        return list(DEFAULT_RISKY_DOMAINS)
    return [str(x) for x in raw if str(x).strip()]


def is_risky_url(url: str, patterns: list[str] | None = None) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    pats = patterns if patterns is not None else load_risky_domains()
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "*").lower()
    host = (parsed.netloc or parsed.path or "").lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    path = (parsed.path or "").lower()
    if any(hint in path for hint in _RISKY_PATH_HINTS):
        return True
    candidate = f"{scheme}://{host}{parsed.path or ''}"
    for pat in pats:
        p = (pat or "").strip().lower()
        if not p:
            continue
        if fnmatch.fnmatch(candidate, p) or fnmatch.fnmatch(f"{scheme}://{host}", p):
            return True
        # Also allow host-only globs like `*.alipay*`.
        host_pat = p.split("://", 1)[-1].split("/", 1)[0]
        if host_pat and fnmatch.fnmatch(host, host_pat):
            return True
    return False


_HOST_RE = re.compile(r"^[a-z0-9.-]+$")


def host_of(url: str) -> str:
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    return host if _HOST_RE.match(host or "") else ""
