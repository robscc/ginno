"""Model provider registry — multi-provider model configs + connectivity verify.

settings.json carries TWO generations of the provider registry (design:
docs/design/multi-provider-model-config.md, decision Q1):

* ``model_configs``  — the v2 array (new writes ONLY land here)::

      "model_configs": [
        {"id": "prov_main", "name": "中转站 A", "protocol": "openai-compatible",
         "base_url": "...", "api_key": "...", "models": ["qwen-plus"],
         "default_model": "qwen-plus", "max_tokens": 8192, "temperature": 0.7,
         "timeout_s": 300, "enabled": true, "verified_at": 1758860000, ...}
      ],
      "default_config": "prov_main"

* ``providers`` — the legacy v1 dict ({pid: cfg}); FROZEN: never written,
  never deleted. When ``model_configs`` is absent the read layer lazily maps
  the legacy slots into an equivalent config array (no disk write), so a
  never-migrated settings.json keeps working and a rollback to an old binary
  is always safe.

Legacy ``default_provider`` is honoured as a fallback for ``default_config``.

Reads merge stored values over CONFIG_DEFAULTS so newly-added fields always
have a sane value. The dict-view wrappers (``load_providers`` /
``save_providers`` / ``get_default_provider``) keep the pre-v2 call signature
for the many existing callers.
"""

from __future__ import annotations

import re
import time
from copy import deepcopy
from typing import Any

from ginno_runtime import jsonx as json

from . import paths
from .lang import t

PROVIDER_IDS = ("anthropic", "openai", "custom")

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "enabled": False,
        "protocol": "anthropic",
        "api_key": "",
        "default_model": "claude-3-7-sonnet-20250219",
        "base_url": "",
        "max_tokens": 4096,
        "temperature": 0.7,
        # 300s default (P2 contract 7): LLM 长回复不再被 60s 掐断；per-provider
        # settings 覆盖能力保留（normalize_config merges stored over defaults）。
        "timeout_s": 300,
        "enable_search": False,
    },
    "openai": {
        "enabled": False,
        "protocol": "openai",
        "api_key": "",
        "default_model": "gpt-4o",
        "base_url": "https://api.openai.com/v1",
        "org_id": "",
        "max_tokens": 8192,
        "enable_search": False,
        "enable_thinking": False,
    },
    "custom": {
        "enabled": False,
        "protocol": "openai-compatible",
        "name": "",
        "api_key": "",
        "base_url": "",
        "model": "",
        "max_tokens": 8192,
        "temperature": 0.7,
        "timeout_s": 300,
        "enable_search": False,
        "enable_thinking": False,
    },
}

# ---- v2 model-config layer -------------------------------------------------

# Wire protocol enums (decision Q6: keep the legacy kebab strings).
CONFIG_PROTOCOLS = ("anthropic", "openai-compatible", "openai-responses")

# Legacy protocol strings → canonical v2 protocols. The legacy "openai" slot
# builds models through the exact same ChatOpenAI path as "openai-compatible"
# (models.py), so it collapses into it.
_PROTOCOL_ALIASES = {
    "anthropic": "anthropic",
    "openai": "openai-compatible",
    "openai-compatible": "openai-compatible",
    "openai_compatible": "openai-compatible",
    "openai-responses": "openai-responses",
    "openai_responses": "openai-responses",
}

# Legacy slot id → default display name (doc §2.6: empty custom name →
# 「自定义端点」).
_LEGACY_SLOT_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "custom": t("Custom endpoint", "自定义端点"),
}

# search_probe 的探针 prompt（模块常量，双语对 (en, zh)）：问一个时效性问题，
# 让用户直观看到 provider 的模型是否真的联网检索；无法联网时要求明说，避免误判。
_SEARCH_PROBE = (
    (
        'Search the web, then answer in one or two sentences: what are today\'s '
        'important news? If you cannot access the internet, explicitly say '
        '"I cannot access the internet".'
    ),
    (
        "请联网检索后，用一两句话回答：今天有哪些重要新闻？"
        "如果你无法联网，请明确说“我无法联网”。"
    ),
)

CONFIG_DEFAULTS: dict[str, Any] = {
    "name": "",
    "protocol": "openai-compatible",
    # Config kind: "chat" (default — a conversational LLM endpoint) or "asr"
    # (speech-to-text, e.g. qwen3-asr-flash via the OpenAI-compatible
    # chat/completions + input_audio shape; consumed by ginno_runtime.asr).
    # ASR configs are excluded from every chat surface (legacy providers view,
    # default chain) — they are only addressable by id from ASR callers.
    "kind": "chat",
    "base_url": "",
    "api_key": "",
    "org_id": "",
    "bearer_auth": False,
    "models": [],
    "default_model": "",
    "max_tokens": 8192,
    "temperature": 0.7,
    # 300s default (P2 contract 7): a stored per-config ``timeout_s`` in
    # settings.json overrides this via normalize_config's merge.
    "timeout_s": 300,
    # Model context window (tokens). 0 = unknown → callers fall back to the
    # name lookup then the mods-level default. This is what $.session.usage()
    # reports as ``context.window`` for reading mods (e.g. token-weather).
    "context_window": 0,
    "enable_search": False,
    "enable_thinking": False,
    # Anthropic protocol only: bind the provider-side `web_search` server tool
    # (web_search_20250305) so the model searches on the gateway instead of
    # through Gin's own DDG/Tavily web_search tool. See models.py
    # `server_web_search_on` and citations.nregister_server_search_hits.
    "server_web_search": False,
    "server_web_search_max_uses": 5,
    "enabled": False,
    "verified_at": None,
    "last_error": None,
}


def _coerce_models(value: Any) -> list[str]:
    """models[] is a list of non-empty strings; drop junk, keep order."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(m).strip() for m in value if str(m).strip()]


def normalize_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a config over CONFIG_DEFAULTS (unknown keys preserved verbatim)."""
    out = deepcopy(CONFIG_DEFAULTS)
    out.update(cfg or {})
    out["models"] = _coerce_models(out.get("models"))
    out["enabled"] = bool(out.get("enabled"))
    # kind 只有两个合法值；未知值一律回 chat（旧配置无此字段也是 chat）。
    out["kind"] = "asr" if str(out.get("kind") or "") == "asr" else "chat"
    return out


def legacy_providers_to_configs(stored: dict[str, Any]) -> list[dict[str, Any]]:
    """Lazily map the frozen legacy ``providers`` dict into v2 config array.

    The three builtin ids come first (defaults merged, exactly like the old
    ``load_providers``); any extra ids the user hand-added follow in stored
    order. Pure function — never touches disk.
    """
    stored = stored or {}
    ordered = list(PROVIDER_IDS) + [pid for pid in stored if pid not in PROVIDER_IDS]
    out: list[dict[str, Any]] = []
    for pid in ordered:
        raw = stored.get(pid)
        if raw is None and pid not in PROVIDER_IDS:
            continue
        merged = deepcopy(PROVIDER_DEFAULTS.get(pid, {}))
        merged.update(raw or {})
        proto = _PROTOCOL_ALIASES.get(str(merged.get("protocol") or ""), None) or (
            "openai-compatible"
        )
        model = str(merged.get("default_model") or merged.get("model") or "").strip()
        models = _coerce_models(merged.get("models")) or ([model] if model else [])
        name = str(merged.get("name") or "").strip() or _LEGACY_SLOT_NAMES.get(pid, pid)
        cfg = normalize_config(merged)
        cfg.update(
            {
                "id": pid,
                "name": name,
                "protocol": proto,
                "models": models,
                "default_model": model or (models[0] if models else ""),
            }
        )
        # legacy single-value model field must not leak into the v2 record
        cfg.pop("model", None)
        out.append(cfg)
    return out


def load_configs(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the v2 config array.

    Source of truth is ``settings["model_configs"]``; when absent (legacy
    install) the frozen ``providers`` dict is lazily mapped instead — read
    only, nothing is written back to disk (decision Q1).
    """
    settings = settings if settings is not None else _read_settings()
    stored = settings.get("model_configs")
    if isinstance(stored, list):
        return [
            normalize_config(c) for c in stored if isinstance(c, dict) and c.get("id")
        ]
    return legacy_providers_to_configs(settings.get("providers") or {})


def validate_configs(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize a full config array for storage; ValueError on bad input."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in configs:
        cfg = normalize_config(raw)
        cid = str(cfg.get("id") or "").strip()
        if not cid:
            raise ValueError(t("Config is missing id", "配置缺少 id"))
        if cid in seen:
            raise ValueError(t(f"Duplicate config id: {cid}", f"配置 id 重复: {cid}"))
        seen.add(cid)
        if cfg["protocol"] not in CONFIG_PROTOCOLS:
            raise ValueError(
                t(
                    f"Config {cid} has invalid protocol: {cfg['protocol']!r}, "
                    f"allowed: {', '.join(CONFIG_PROTOCOLS)}",
                    f"配置 {cid} 协议非法: {cfg['protocol']!r}，"
                    f"可选值: {', '.join(CONFIG_PROTOCOLS)}",
                )
            )
        if cfg["default_model"] and cfg["models"] and cfg["default_model"] not in cfg["models"]:
            # Case-only mismatch → REPAIR to the listed spelling instead of
            # refusing the save. Model ids are lowercase on several gateways while
            # a hand-written or migrated config may use mixed case; rejecting the
            # whole PUT left the provider's default permanently unsaveable from
            # the UI (settings shows the error, nothing the user can do about it).
            _matched = next(
                (m for m in cfg["models"] if m.lower() == cfg["default_model"].lower()),
                None,
            )
            if _matched is not None:
                cfg["default_model"] = _matched
            else:
                raise ValueError(
                    t(
                        f"Config {cid}'s default_model {cfg['default_model']!r} "
                        f"is not in models: {', '.join(cfg['models'])}",
                        f"配置 {cid} 的 default_model {cfg['default_model']!r} "
                        f"不在 models 内: {', '.join(cfg['models'])}",
                    )
                )
        cfg["id"] = cid
        out.append(cfg)
    return out


def save_configs(
    configs: list[dict[str, Any]], default_config: str | None = None
) -> list[dict[str, Any]]:
    """Persist the v2 array (full replacement) under ``model_configs``.

    The legacy ``providers`` / ``default_provider`` keys are NEVER touched
    (decision Q1: frozen for read-only compat with old binaries).
    """
    normalized = validate_configs(configs)
    settings = _read_settings()
    settings["model_configs"] = normalized
    if default_config is not None:
        settings["default_config"] = default_config
    _write_settings(settings)
    return normalized


# Model-name → context window (tokens), used when a config declares no explicit
# ``context_window``. Substring match; conservative builtin defaults.
_CONTEXT_WINDOW_BY_MODEL: tuple[tuple[str, int], ...] = (
    ("claude", 200_000),
    ("gpt-3.5", 16_385),
    ("gpt-4", 128_000),
    ("gemini", 1_000_000),
)


def context_window_for(
    config: dict[str, Any] | None, model_name: str | None = None
) -> int:
    """The context window (tokens) the runtime knows for a session's model.

    Order: the model config's explicit ``context_window`` (Settings → Model API)
    → a model-name lookup → ``0`` (unknown; the caller decides the final
    fallback). Reading mods (e.g. token-weather) get this via
    ``$.session.usage().context.window``.
    """
    try:
        cw = int((config or {}).get("context_window") or 0)
        if cw > 0:
            return cw
    except (TypeError, ValueError):
        pass
    name = (model_name or (config or {}).get("default_model") or "").lower()
    for pat, win in _CONTEXT_WINDOW_BY_MODEL:
        if pat in name:
            return win
    return 0


def get_config(config_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for cfg in load_configs(settings):
        if cfg["id"] == config_id:
            return cfg
    return None


def get_default_config(settings: dict[str, Any] | None = None) -> str:
    """The global default config id.

    Explicit ``default_config`` (falling back to legacy ``default_provider``)
    if it names an enabled CHAT config (asr kind can't drive chat); otherwise
    the first enabled chat config — scanning the WHOLE array, not just the
    three legacy slots; otherwise the configured choice as-is (it will surface
    as an error later, same as before).
    """
    settings = settings if settings is not None else _read_settings()
    configs = load_configs(settings)
    by_id = {c["id"]: c for c in configs}
    chosen = settings.get("default_config") or settings.get("default_provider")
    if (
        chosen in by_id
        and by_id[chosen].get("enabled")
        and by_id[chosen].get("kind") != "asr"
    ):
        return chosen
    for cfg in configs:
        if cfg.get("enabled") and cfg.get("kind") != "asr":
            return cfg["id"]
    return chosen or "custom"


def model_for_config(cfg: dict[str, Any] | None) -> str:
    """The config's default model (``default_model``, then ``models[0]``)."""
    if not cfg:
        return ""
    models = cfg.get("models") or []
    return cfg.get("default_model") or (models[0] if models else "")


# ---- legacy dict view (thin wrappers over the v2 layer) ---------------------


def load_providers(settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Pre-v2 dict view {config_id: config} of :func:`load_configs`.

    Kept as a thin wrapper so every existing caller (sessions.py, config.py,
    models.py, …) and its monkeypatch-based tests keep working unchanged.
    kind="asr" configs are EXCLUDED — every consumer of this view is a chat
    surface (session binding, agents dropdown, default chain); ASR configs
    are addressed by id through ginno_runtime.asr instead.
    """
    return {cfg["id"]: cfg for cfg in load_configs(settings)
            if cfg.get("kind") != "asr"}


def save_providers(providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Legacy dict-form save: converts the payload to configs and MERGES it
    into the stored array by id (configs absent from the payload survive —
    the legacy dict form cannot express them)."""
    drafts = legacy_providers_to_configs(providers)
    merged = {cfg["id"]: cfg for cfg in load_configs()}
    for cfg in drafts:
        merged[cfg["id"]] = cfg
    saved = save_configs(list(merged.values()))
    return {cfg["id"]: cfg for cfg in saved}


def get_default_provider(settings: dict[str, Any] | None = None) -> str:
    return get_default_config(settings)


def model_for_provider(providers: dict[str, dict[str, Any]], pid: str) -> str:
    cfg = providers.get(pid, {})
    return cfg.get("default_model") or cfg.get("model") or ""


# ---- system-proxy switch (settings.json top-level `use_system_proxy`) ------


def use_system_proxy(settings: dict[str, Any] | None = None) -> bool:
    """Whether model traffic should honour OS/environment proxies.

    Defaults to True (standard behaviour for remote endpoints). Loopback
    destinations bypass unconditionally regardless — see
    ``_ensure_loopback_no_proxy`` in the package ``__init__``.
    """
    settings = settings if settings is not None else _read_settings()
    return bool(settings.get("use_system_proxy", True))


def apply_system_proxy(enabled: bool) -> None:
    """Make the switch effective for every httpx-based client in this process.

    All LLM traffic (anthropic/openai SDKs, langchain ChatAnthropic /
    ChatOpenAI) rides httpx with ``trust_env=True``; each client freezes its
    proxy map at init from ``httpx._client.get_environment_proxies`` — a
    module-global name resolved at call time. Swapping that one name is the
    process-wide choke point; there is no per-client wiring for
    ChatAnthropic anyway (langchain_anthropic builds its httpx client
    internally and shares one ``functools.lru_cache``'d instance per
    (base_url, timeout)).

    Already-built clients keep their frozen proxy map, so callers must also
    evict whatever caches still hold them (``_SESSIONS``); the lru_cache
    clear below covers the shared langchain_anthropic clients.
    """
    import httpx._client as _hx_client
    import httpx._utils as _hx_utils

    if enabled:
        _hx_client.get_environment_proxies = _hx_utils.get_environment_proxies
    else:
        _hx_client.get_environment_proxies = lambda: {}
    # langchain_anthropic shares one lru_cache'd httpx client across ALL
    # ChatAnthropic instances — drop both (sync/async) so the next model
    # rebuild picks up the new proxy map.
    try:
        from langchain_anthropic import _client_utils as _lc_cu

        _lc_cu._get_default_httpx_client.cache_clear()
        _lc_cu._get_default_async_httpx_client.cache_clear()
    except Exception:  # noqa: BLE001 — version drift must not break settings
        pass


def _read_settings() -> dict[str, Any]:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def _write_settings(settings: dict[str, Any]) -> None:
    paths.settings_path().write_text(
        json.dumps(settings, indent=2, ensure_ascii=False)
    )


# ---- connectivity verify ----------------------------------------------------


_WIRE_FIELD_LABELS: dict[str, str] = {
    "api_key": "API Key",
    "org_id": "Organization ID",
    "base_url": "Base URL",
}


def _non_ascii_wire_fields(cfg: dict[str, Any]) -> list[str]:
    """上线路径字段（api_key/org_id/base_url）中含非 ASCII 字符的字段名列表。

    这些值最终进 HTTP header 或 URL；httpx 按 ASCII 编码 header，任何
    codepoint > 127 的字符（中文/日韩文、emoji、全角符号、NBSP、BOM、
    零宽字符……粘贴事故的常见载荷）都会炸出 UnicodeEncodeError 天书
    （2026-10-29 真机：拉取模型列表报 'ascii' codec can't encode
    characters）。请求前拦截，报错直接指认字段，而不是让用户看编码器
    内部错误。
    """
    bad: list[str] = []
    for field in _WIRE_FIELD_LABELS:
        val = str(cfg.get(field) or "")
        if val and not val.isascii():
            bad.append(field)
    return bad


def _first_non_ascii_char(s: str) -> str:
    """首个非 ASCII 字符的展示形式 ``'\\xa0' (U+00A0)``。

    不可见字符（NBSP/BOM/零宽）肉眼查不出来，报码位才能定位；``!r``
    让控制字符显示为转义序列而不是空白。
    """
    for ch in s:
        if not ch.isascii():
            return f"{ch!r} (U+{ord(ch):04X})"
    return "?"


def _non_ascii_wire_error(cfg: dict[str, Any], fields: list[str]) -> str:
    labels = "、".join(_WIRE_FIELD_LABELS.get(f, f) for f in fields)
    first = _first_non_ascii_char(str(cfg.get(fields[0]) or ""))
    return t(
        f"Non-ASCII characters found in {labels} (first: {first}). These "
        "fields only accept ASCII — the pasted text may carry full-width "
        "punctuation, emoji, or invisible characters.",
        f"{labels} 含非 ASCII 字符（首个：{first}）。这些字段只接受 ASCII "
        "字符——粘贴的内容可能带入了全角符号、emoji 或不可见字符。",
    )


def _verify_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Probe one config (draft or saved) with the cheapest possible call.

    The network seam for tests — monkeypatch this (or the SDK clients it
    imports inside the branches). Never raises.
    """
    proto = cfg.get("protocol")
    # Interactive settings-page probe: bounded independently of the chat
    # timeout — this runs synchronously inside the asyncio event loop
    # (api/config.py verify_model_config_endpoint), so the P2 default of 300s
    # against a black-holed base_url would freeze EVERY session for 5 minutes.
    # 60s preserves the pre-P2 effective bound.
    timeout = min(float(cfg.get("timeout_s") or 300), 60.0)
    t0 = time.time()

    def _latency() -> int:
        return int((time.time() - t0) * 1000)

    bad = _non_ascii_wire_fields(cfg)
    if bad:
        return {"ok": False, "error": _non_ascii_wire_error(cfg, bad),
                "latency_ms": _latency()}

    try:
        if proto == "anthropic":
            if not cfg.get("api_key"):
                return {"ok": False, "error": t("API key is empty", "API Key 为空"),
                        "latency_ms": _latency()}
            import anthropic

            client_kw: dict[str, Any] = {
                "base_url": cfg.get("base_url") or None,
                "timeout": timeout,
            }
            if cfg.get("bearer_auth"):
                client_kw["auth_token"] = cfg["api_key"]  # Authorization: Bearer
            else:
                client_kw["api_key"] = cfg["api_key"]  # x-api-key
            client = anthropic.Anthropic(**client_kw)
            client.messages.create(
                model=model_for_config(cfg) or "claude-3-7-sonnet-20250219",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return {"ok": True, "latency_ms": _latency()}

        if proto == "openai-responses":
            import openai

            base = cfg.get("base_url") or "https://api.openai.com/v1"
            client = openai.OpenAI(
                api_key=cfg.get("api_key") or "not-needed",
                base_url=base,
                timeout=timeout,
                **({"organization": cfg["org_id"]} if cfg.get("org_id") else {}),
            )
            # Responses API has no /models listing — one minimal generation
            # (max_output_tokens=16) is the cheapest liveness probe.
            client.responses.create(
                model=model_for_config(cfg) or "gpt-4o",
                max_output_tokens=16,
                input="ping",
            )
            return {"ok": True, "latency_ms": _latency()}

        # openai-compatible (and the legacy "openai" slot, same wire family)
        import openai

        base = cfg.get("base_url") or None
        if not base:
            base = "https://api.openai.com/v1"
        client = openai.OpenAI(
            api_key=cfg.get("api_key") or "not-needed",
            base_url=base,
            timeout=timeout,
        )
        try:
            client.models.list()
        except Exception:
            # some compatible endpoints lack /models — fall back to a 1-token chat
            client.chat.completions.create(
                model=model_for_config(cfg) or "gpt-4o",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        return {"ok": True, "latency_ms": _latency()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": _latency()}


def verify_config_draft(cfg: dict[str, Any]) -> dict[str, Any]:
    """Verify a config DRAFT (not yet saved). Never raises, never writes."""
    return _verify_config(normalize_config(cfg))


# ---- model listing (「从 API 拉取模型」) -------------------------------------


def _list_models(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fetch a config's model catalogue from its list-models endpoint.

    The network seam for tests — monkeypatch this (or ``httpx.get``).
    Normalizes the wire responses into ``{id, owned_by}`` rows sorted by id
    (anthropic's ``display_name`` rides along as ``owned_by`` — it is the
    only secondary label that protocol offers). Never raises; failures come
    back as ``{ok: False, error}`` with a one-line Chinese message, same
    convention as :func:`_verify_config`. Fixed short timeout: this is an
    interactive settings-page call, not LLM traffic.
    """
    import httpx

    t0 = time.time()

    def _latency() -> int:
        return int((time.time() - t0) * 1000)

    proto = cfg.get("protocol")
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "error": t("API key is empty", "API Key 为空"),
                "latency_ms": _latency()}
    bad = _non_ascii_wire_fields(cfg)
    if bad:
        # 同 _verify_config：非 ASCII 字符在 httpx header 编码处炸
        # UnicodeEncodeError，这里提前指认字段。
        return {"ok": False, "error": _non_ascii_wire_error(cfg, bad),
                "latency_ms": _latency()}
    # trailing "/" would double up in the f-string below
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    headers: dict[str, str] = {}
    if proto == "anthropic":
        base = base or "https://api.anthropic.com"
        # gateways may or may not include /v1 in base_url — never double-append
        if not base.endswith("/v1"):
            base += "/v1"
        url = f"{base}/models"
        if cfg.get("bearer_auth"):
            headers["Authorization"] = f"Bearer {key}"  # same switch as verify
        else:
            headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    else:
        # openai-compatible + openai-responses share the OpenAI /models shape
        base = base or "https://api.openai.com/v1"
        url = f"{base}/models"
        headers["Authorization"] = f"Bearer {key}"
        if cfg.get("org_id"):
            headers["OpenAI-Organization"] = str(cfg["org_id"])

    try:
        resp = httpx.get(url, headers=headers, timeout=8.0)
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": t(f"Failed to list models (HTTP {resp.status_code})",
                           f"拉取模型列表失败（HTTP {resp.status_code}）"),
                "latency_ms": _latency(),
            }
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {
                "ok": False,
                "error": t("Failed to list models: response has no data list, "
                           "cannot parse",
                           "拉取模型列表失败：响应缺少 data 列表，无法解析"),
                "latency_ms": _latency(),
            }
        models: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("id") or "").strip()
            if not mid:
                continue
            owned = str(row.get("owned_by") or row.get("display_name") or "").strip()
            models.append({"id": mid, "owned_by": owned or None})
        models.sort(key=lambda m: m["id"])
        return {"ok": True, "models": models, "latency_ms": _latency()}
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": t(f"Failed to list models: {type(e).__name__}: {e}",
                       f"拉取模型列表失败：{type(e).__name__}: {e}"),
            "latency_ms": _latency(),
        }


def list_models_draft(cfg: dict[str, Any]) -> dict[str, Any]:
    """List models for a config DRAFT (not yet saved). Never raises, never writes."""
    return _list_models(normalize_config(cfg))


def verify(provider_id: str) -> dict[str, Any]:
    """Probe a saved provider/config with the cheapest possible call. Never raises."""
    cfg = get_config(provider_id)
    if not cfg:
        return {"ok": False, "error": f"unknown provider: {provider_id}"}
    return _verify_config(cfg)


def search_probe(provider_id: str) -> dict[str, Any]:
    """Send one time-sensitive question with ``enable_search`` forced on and
    return the model's reply, so the user can see whether the provider's model
    actually searches the web. Never raises. Only meaningful for protocols that
    honour ``enable_search`` (OpenAI-compatible); on others the model simply
    answers from its own knowledge (which the user can eyeball)."""
    cfg = get_config(provider_id)
    if not cfg:
        return {"ok": False, "error": f"unknown provider: {provider_id}"}
    if not cfg.get("enabled"):
        return {"ok": False, "error": t("Provider not enabled", "provider 未启用")}
    t0 = time.time()

    def _latency() -> int:
        return int((time.time() - t0) * 1000)

    try:
        from .models import build_model  # local: models imports this module

        model = build_model(provider_id, enable_search=True)
        res = model.invoke(
            [
                {
                    "role": "user",
                    "content": t(*_SEARCH_PROBE),
                }
            ]
        )
        text = getattr(res, "content", "") or ""
        if isinstance(text, list):  # multimodal content blocks
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        # Usage telemetry (usage-stats-design §3.6): the search probe is a real
        # billed completion, so it gets its own bucket instead of vanishing.
        try:
            from . import usage as usage_mod
            from . import usage_store

            u = usage_mod.extract_usage(res) or {}
            if u:
                usage_store.record(
                    input_tokens=u.get("input_tokens") or 0,
                    output_tokens=u.get("output_tokens") or 0,
                    cache_read_tokens=u.get("cache_read_tokens") or 0,
                    cache_creation_tokens=u.get("cache_creation_tokens") or 0,
                    provider=provider_id,
                    model=str(getattr(model, "model_name", "") or model_for_config(cfg)),
                    source="probe",
                    latency_ms=_latency(),
                )
        except Exception:  # noqa: BLE001 — telemetry never breaks the probe
            pass
        return {"ok": True, "latency_ms": _latency(), "text": str(text).strip()[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": _latency()}


# ---- secret masking (SEC-01) --------------------------------------------- #
# GET endpoints must not hand back provider keys in plaintext: anything that
# can reach 127.0.0.1:8787 (or run script in the WebView) would otherwise be
# able to exfiltrate them. The mask keeps the last 4 characters for
# recognisability; PUT handlers restore the stored value whenever they see a
# masked one, so a load-modify-save round-trip (e.g. the proxy toggle, which
# PUTs the whole settings document) can never overwrite a real key.

MASK_PREFIX = "••••"
_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)
# 精确命中才算 secret 字段（不能用上面的正则扫任意 dict 键：max_tokens
# 含 "TOKEN" 会被误掩）。settings 里除 env/model_configs/providers 外的
# secret 段（classifier/web/channels/connectors）按这些字段名掩码。
_SECRET_FIELD_NAMES = frozenset({
    "api_key", "asr_api_key", "corp_secret", "secret", "secret_key",
    "aes_key", "token", "password",
})


def mask_secret(value: Any) -> Any:
    s = str(value or "")
    if not s:
        return value
    return MASK_PREFIX + s[-4:] if len(s) > 4 else MASK_PREFIX


def is_masked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MASK_PREFIX)


def _mask_secret_leaves(node: Any) -> None:
    """就地掩码 secret 字段名命中的叶子（递归 dict/list）。

    掩码体系的原则（SEC-01 + 2026-10-29 review）：掩码只允许存在于
    「GET 响应 → 表单显示」这一段；落盘与出网请求永远只见真实值。
    真实消费方（classifier/laya、web/engines、channels）都从磁盘
    load，掩码不进它们的路径。
    """
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, str) and v and k in _SECRET_FIELD_NAMES:
                node[k] = mask_secret(v)
            else:
                _mask_secret_leaves(v)
    elif isinstance(node, list):
        for item in node:
            _mask_secret_leaves(item)


def clear_masked_leaves(node: Any, path: str = "") -> list[str]:
    """写盘前把残留的掩码叶子置空，返回被清掉的路径（供日志）。

    语义（2026-10-29 review 定稿）：入站 payload 里的掩码值 = 「未修改」
    占位——unmask_* 能还原的已还原；还原不了的（存储里没有对应条目，
    如复制配置换了 id）置空，绝不把 ``••••`` 写进 settings.json，否则
    真实流量会带着掩码出网，在 httpx ASCII 编码处炸 UnicodeEncodeError
    （2026-10-24 智谱 key 事故同型）。保存不因占位值报错。
    """
    cleared: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and is_masked(v):
                node[k] = ""
                cleared.append(p)
            else:
                cleared.extend(clear_masked_leaves(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            cleared.extend(clear_masked_leaves(v, f"{path}[{i}]"))
    return cleared


def _mask_config_list(items: Any) -> None:
    for c in items or []:
        if isinstance(c, dict) and c.get("api_key"):
            c["api_key"] = mask_secret(c["api_key"])


def mask_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Copy of a settings document with provider/env secrets masked."""
    out = deepcopy(settings or {})
    env = out.get("env")
    if isinstance(env, dict):
        for k, v in list(env.items()):
            if isinstance(v, str) and v and _SECRET_NAME.search(str(k)):
                env[k] = mask_secret(v)
    _mask_config_list(out.get("model_configs"))
    provs = out.get("providers")
    if isinstance(provs, dict):
        _mask_config_list(provs.values())
    # 2026-10-29 review 补覆盖：这些段此前 GET 明文回传（classifier 的
    # SF 网关 token、tavily key、连接器 secret 都真实存在过）。还原靠
    # _restore_masked 的同路径回填，写盘残留由 find_masked_leaves 拦截。
    for section in ("classifier", "web", "channels", "connectors"):
        node = out.get(section)
        if isinstance(node, (dict, list)):
            _mask_secret_leaves(node)
    return out


def _restore_masked(new: Any, old: Any) -> Any:
    """Replace masked leaves in ``new`` with the corresponding ``old`` values."""
    if isinstance(new, dict) and isinstance(old, dict):
        for k, v in list(new.items()):
            if is_masked(v) and old.get(k) is not None:
                new[k] = old[k]
            elif isinstance(v, (dict, list)):
                new[k] = _restore_masked(v, old.get(k))
        return new
    if isinstance(new, list) and isinstance(old, list):
        old_by_id = {
            o.get("id"): o for o in old if isinstance(o, dict) and o.get("id")
        }
        return [
            _restore_masked(n, old_by_id.get(n.get("id")) if isinstance(n, dict) else None)
            for n in new
        ]
    return new


def unmask_settings(data: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`mask_settings` for a PUT body."""
    return _restore_masked(deepcopy(data or {}), previous or {})


def unmask_config_draft(cfg: dict[str, Any]) -> dict[str, Any]:
    """Restore a masked ``api_key`` on a verify/list-models draft from storage."""
    out = dict(cfg or {})
    if not is_masked(out.get("api_key")):
        return out
    stored = {c.get("id"): c for c in load_configs()}
    prev = stored.get(out.get("id")) or {}
    if prev.get("api_key"):
        out["api_key"] = prev["api_key"]
    return out
