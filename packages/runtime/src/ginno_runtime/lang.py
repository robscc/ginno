"""Locale resolution + bilingual string selection — the i18n core.

``language`` in settings.json: ``"auto"`` (default) | ``"en"`` | ``zh-CN``.
The legacy ``prompt_language`` key is merged into it once at settings load
(:func:`ginno_runtime.paths._migrate_language`, i18n-design.md §2).

``current_locale()`` priority (i18n-design.md §1/§5):

1. Request binding — the pure-ASGI middleware in ``server.py`` reads the
   ``X-Ginno-Language`` header (valid values ``en`` | ``zh-CN``, anything
   else is ignored) and binds a contextvar for the whole HTTP request / WS
   connection. asyncio tasks spawned inside a request (turn jobs,
   ``spawn_bg``) inherit the binding via context copy, so it stays effective
   for the entire turn.
2. Settings resolution — background jobs (scheduler, notifications) run
   outside any request and fall through to settings.json, read on *every*
   call so a settings write takes effect without a restart (the file is
   tiny and this runs at prompt-assembly frequency, not per token).
3. ``GINNO_LANGUAGE`` env overrides both (tests / dev); ``zh`` / ``zh-CN``
   are both accepted spellings.

Use ``t(en, zh)`` for model-facing strings (system prompts, subagent briefs,
tool descriptions, tool-result errors/hints); use
``ginno_runtime.i18n._(key, ...)`` for user-facing catalog strings. Logs
stay English — never route them through ``t()``/``_()``.
"""

from __future__ import annotations

import contextvars
import json
import locale as _locale_mod
import logging
import os
import warnings

from . import paths

_log = logging.getLogger(__name__)

# Canonical locales; "zh-CN" is the only Chinese variant (no zh-TW/zh-HK).
SUPPORTED_LOCALES = ("en", "zh-CN")

# Request-scoped locale. None = no valid header bound in this context →
# fall through to settings resolution (background jobs live here).
_locale_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ginno_locale", default=None
)


def _normalize_locale(value: str | None) -> str | None:
    """Case-insensitive locale match: ``ZH``/``zh``/``zh-cn`` → ``zh-CN``,
    ``EN`` → ``en``, anything else → None (invalid = ignored, never an error).
    """
    if not value:
        return None
    low = value.strip().lower()
    if low == "en":
        return "en"
    if low in ("zh", "zh-cn"):
        return "zh-CN"
    return None


def bind_request_locale(value: str | None):
    """Bind the request locale in the current context.

    Returns the token for :func:`reset_request_locale`, or ``None`` when the
    value is not a supported locale (invalid headers are ignored by design —
    never an error).
    """
    normalized = _normalize_locale(value)
    if normalized is not None:
        return _locale_var.set(normalized)
    return None


def reset_request_locale(token) -> None:
    """Undo a :func:`bind_request_locale` (no-op for a skipped bind)."""
    if token is not None:
        _locale_var.reset(token)


def _auto_locale() -> str:
    """``auto`` → OS locale: anything starting with ``zh`` → zh-CN, else en.

    Unsupported system languages (ja, fr, …) silently land on en and never
    bother the user (i18n-design.md §2).
    """
    try:
        # getdefaultlocale is deprecated (removal slated for 3.15) but is
        # exactly the "what language is this machine" probe we want; silence
        # the warning so sidecar.log stays clean.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            loc = _locale_mod.getdefaultlocale()[0]
        return "zh-CN" if (loc or "").lower().startswith("zh") else "en"
    except Exception:
        return "en"


def settings_locale() -> str:
    """Effective locale from settings (``language``, fallback per §2).

    Reads the file on every call so a settings write takes effect without a
    restart. ``GINNO_LANGUAGE`` env wins (tests / dev; ``zh`` accepted as a
    ``zh-CN`` spelling).
    """
    from_env = _normalize_locale(os.environ.get("GINNO_LANGUAGE"))
    if from_env is not None:
        return from_env
    try:
        p = paths.settings_path()
        if p.exists():
            settings = json.loads(p.read_text() or "{}")
            val = settings.get("language") if isinstance(settings, dict) else None
            if isinstance(val, str) and val.strip().lower() != "auto":
                normalized = _normalize_locale(val)
                if normalized is not None:
                    return normalized
            # "auto" (default), missing, or unknown value → auto-resolve
            return _auto_locale()
    except (OSError, ValueError):
        _log.info("settings_locale_unreadable", exc_info=True)
    return _auto_locale()


def current_locale() -> str:
    """Request locale when bound, else the settings-resolved locale."""
    bound = _locale_var.get()
    if bound in SUPPORTED_LOCALES:
        return bound
    return settings_locale()


def t(en: str, zh: str) -> str:
    """Pick the bilingual variant for the current locale (zh-CN → zh)."""
    return zh if current_locale() == "zh-CN" else en


# 模型回复语言指令块（i18n-design.md §6）：跟随当前 locale 选边。合并
# prompt_language 之后 UI 语言与回复语言一致，不再存在组合分歧。
# 两条常量都用英文措辞（system prompt 惯例），zh 变体点名「简体中文」。
_RESPONSE_DIRECTIVE_EN = "Always respond in English."
_RESPONSE_DIRECTIVE_ZH = "Always respond in Simplified Chinese (简体中文)."


def response_lang_directive() -> str:
    """System-prompt instruction pinning the model's reply language to the
    active locale (constants above, selected by :func:`current_locale`)."""
    return t(_RESPONSE_DIRECTIVE_EN, _RESPONSE_DIRECTIVE_ZH)
