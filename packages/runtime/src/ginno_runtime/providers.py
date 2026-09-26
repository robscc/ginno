"""Model provider registry — multi-provider model configs + connectivity verify.

settings.json carries TWO generations of the provider registry (design:
docs/design/multi-provider-model-config.md, decision Q1):

* ``model_configs``  — the v2 array (new writes ONLY land here)::

      "model_configs": [
        {"id": "prov_main", "name": "中转站 A", "protocol": "openai-compatible",
         "base_url": "...", "api_key": "...", "models": ["qwen-plus"],
         "default_model": "qwen-plus", "max_tokens": 8192, "temperature": 0.7,
         "timeout_s": 60, "enabled": true, "verified_at": 1758860000, ...}
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

import json
import time
from copy import deepcopy
from typing import Any

from . import paths

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
        "timeout_s": 60,
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
        "timeout_s": 60,
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
    "custom": "自定义端点",
}

CONFIG_DEFAULTS: dict[str, Any] = {
    "name": "",
    "protocol": "openai-compatible",
    "base_url": "",
    "api_key": "",
    "org_id": "",
    "bearer_auth": False,
    "models": [],
    "default_model": "",
    "max_tokens": 8192,
    "temperature": 0.7,
    "timeout_s": 60,
    "enable_search": False,
    "enable_thinking": False,
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
            raise ValueError("配置缺少 id")
        if cid in seen:
            raise ValueError(f"配置 id 重复: {cid}")
        seen.add(cid)
        if cfg["protocol"] not in CONFIG_PROTOCOLS:
            raise ValueError(
                f"配置 {cid} 协议非法: {cfg['protocol']!r}，"
                f"可选值: {', '.join(CONFIG_PROTOCOLS)}"
            )
        if cfg["default_model"] and cfg["models"] and cfg["default_model"] not in cfg["models"]:
            raise ValueError(
                f"配置 {cid} 的 default_model {cfg['default_model']!r} "
                f"不在 models 内: {', '.join(cfg['models'])}"
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


def get_config(config_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any] | None:
    for cfg in load_configs(settings):
        if cfg["id"] == config_id:
            return cfg
    return None


def get_default_config(settings: dict[str, Any] | None = None) -> str:
    """The global default config id.

    Explicit ``default_config`` (falling back to legacy ``default_provider``)
    if it names an enabled config; otherwise the first enabled config —
    scanning the WHOLE array, not just the three legacy slots; otherwise the
    configured choice as-is (it will surface as an error later, same as before).
    """
    settings = settings if settings is not None else _read_settings()
    configs = load_configs(settings)
    by_id = {c["id"]: c for c in configs}
    chosen = settings.get("default_config") or settings.get("default_provider")
    if chosen in by_id and by_id[chosen].get("enabled"):
        return chosen
    for cfg in configs:
        if cfg.get("enabled"):
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
    """
    return {cfg["id"]: cfg for cfg in load_configs(settings)}


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


def _verify_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Probe one config (draft or saved) with the cheapest possible call.

    The network seam for tests — monkeypatch this (or the SDK clients it
    imports inside the branches). Never raises.
    """
    proto = cfg.get("protocol")
    timeout = float(cfg.get("timeout_s") or 60)
    t0 = time.time()

    def _latency() -> int:
        return int((time.time() - t0) * 1000)

    try:
        if proto == "anthropic":
            if not cfg.get("api_key"):
                return {"ok": False, "error": "API Key 为空", "latency_ms": _latency()}
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
        return {"ok": False, "error": "API Key 为空", "latency_ms": _latency()}
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
                "error": f"拉取模型列表失败（HTTP {resp.status_code}）",
                "latency_ms": _latency(),
            }
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {
                "ok": False,
                "error": "拉取模型列表失败：响应缺少 data 列表，无法解析",
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
            "error": f"拉取模型列表失败：{type(e).__name__}: {e}",
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
        return {"ok": False, "error": "provider 未启用"}
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
                    "content": (
                        "请联网检索后，用一两句话回答：今天有哪些重要新闻？"
                        "如果你无法联网，请明确说“我无法联网”。"
                    ),
                }
            ]
        )
        text = getattr(res, "content", "") or ""
        if isinstance(text, list):  # multimodal content blocks
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        return {"ok": True, "latency_ms": _latency(), "text": str(text).strip()[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": _latency()}
