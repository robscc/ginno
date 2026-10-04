"""Catalog-based user-facing strings (i18n-design.md §5 / §9).

Inline ``t(en, zh)`` stays for prompt-template prose (copy as code); this
package serves the structured short copy (error codes, status lines,
notification titles) as ``key + params`` with catalogs shipped next to the
code:

    ginno_runtime/i18n/en.json       # source of truth (en)
    ginno_runtime/i18n/zh_CN.json

``_(key, **params)`` resolves against the current locale with the fallback
chain 请求 locale → en → the key itself; a miss logs ``WARNING
i18n_missing`` (sidecar.log). ``{name}`` placeholders are filled from
params; unknown params render as the bare ``{name}`` so a params/catalog
skew degrades visibly instead of raising.

Logs stay English — this module is for user-facing output only.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

from ..lang import current_locale

_log = logging.getLogger(__name__)

# ICU-style placeholder: {name} (letter/underscore first). ``{{escaped}}``
# brace literals are intentionally not part of the grammar — the catalogs
# are plain key/value strings, not full ICU messages.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_CATALOG_FILES = {"en": "en.json", "zh-CN": "zh_CN.json"}

# locale -> loaded catalog dict, or a cached RuntimeError (a missing bundle
# file must not retry on every call). Tests may clear this to force a reload.
_catalogs: dict[str, object] = {}


class _SafeParams(dict):
    """format_map backend: unknown params render as the bare ``{placeholder}``."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _catalog_dir() -> Path:
    """Package dir in dev / installed wheels; the ``_MEIPASS`` copy in
    PyInstaller bundles (the spec/Makefile puts the JSONs at
    ``_MEIPASS/ginno_runtime/i18n``, mirroring skills/builtin)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "ginno_runtime" / "i18n"
    return Path(__file__).resolve().parent


def _load_catalog(locale: str) -> dict[str, str]:
    """Read one catalog fresh from disk. Raises RuntimeError when unusable."""
    fname = _CATALOG_FILES.get(locale)
    if not fname:
        raise RuntimeError(f"unsupported locale: {locale}")
    data = None
    try:
        # importlib.resources keeps this a proper package resource in dev;
        # frozen builds store pure modules inside the PYZ where the JSON is
        # NOT addressable, so fall back to the _MEIPASS copy.
        from importlib import resources

        data = json.loads(
            resources.files("ginno_runtime.i18n").joinpath(fname).read_text("utf-8")
        )
    except Exception:
        data = None
    if not isinstance(data, dict):
        try:
            data = json.loads((_catalog_dir() / fname).read_text("utf-8"))
        except Exception as e:
            raise RuntimeError(f"cannot load catalog {locale} ({fname}): {e}") from e
    return {str(k): str(v) for k, v in data.items()}


def _catalog(locale: str) -> dict[str, str]:
    """Cached catalog accessor; raises the cached RuntimeError on repeat
    failures so a broken bundle errors once per call site, not per key."""
    cached = _catalogs.get(locale)
    if cached is not None:
        if isinstance(cached, RuntimeError):
            raise cached
        return cached  # type: ignore[return-value]
    try:
        cat: dict[str, str] = _load_catalog(locale)
    except RuntimeError as e:
        _catalogs[locale] = e
        raise
    _catalogs[locale] = cat
    return cat


def _(key: str, **params) -> str:
    """Translate ``key`` in the current locale; fallback chain
    请求 locale → en → the key itself (never raises to the caller)."""
    locale = current_locale()
    text: str | None = None
    for loc in (locale, "en"):
        if text is not None:
            break
        try:
            text = _catalog(loc).get(key)
        except RuntimeError as e:
            # surfaced loudly by i18n_health_check at startup; degrade here
            _log.error("i18n_catalog_load_failed locale=%s err=%s", loc, e)
    if text is None:
        # Fallback chain exhausted: show the raw key + leave a trace.
        _log.warning("i18n_missing key=%s locale=%s", key, locale)
        return key
    if params:
        text = text.format_map(_SafeParams(**params))
    return text


def i18n_health_check() -> list[str]:
    """Startup bundle check (i18n-design.md §10.5): both catalogs must load,
    key sets must match (both directions), and every shared key must use the
    same ICU placeholders. Returns a list of concrete problems (empty =
    healthy); never raises — the caller logs ``ERROR i18n_bundle`` and
    continues so a packaging gap cannot crash the app, only degrade it.
    """
    problems: list[str] = []
    cats: dict[str, dict[str, str]] = {}
    for locale in ("en", "zh-CN"):
        try:
            cats[locale] = _load_catalog(locale)
        except RuntimeError as e:
            problems.append(f"{locale} catalog failed to load: {e}")
    if len(cats) == 2:
        en_keys, zh_keys = set(cats["en"]), set(cats["zh-CN"])
        missing_zh = sorted(en_keys - zh_keys)
        extra_zh = sorted(zh_keys - en_keys)
        if missing_zh:
            problems.append("keys missing in zh-CN: " + ", ".join(missing_zh))
        if extra_zh:
            problems.append("keys missing in en: " + ", ".join(extra_zh))
        for key in sorted(en_keys & zh_keys):
            pe = sorted(set(_PLACEHOLDER_RE.findall(cats["en"][key])))
            pz = sorted(set(_PLACEHOLDER_RE.findall(cats["zh-CN"][key])))
            if pe != pz:
                problems.append(f"placeholder mismatch for {key}: en={pe} zh-CN={pz}")
    return problems
