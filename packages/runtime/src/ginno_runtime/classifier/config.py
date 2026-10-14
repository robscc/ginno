# -*- coding: utf-8 -*-
"""Classifier middleware configuration (settings.json top-level ``classifier``).

Default OFF — the whole layer is inert unless the user opts in. Shape:

.. code-block:: json

    {
      "classifier": {
        "mode": "off",              // off | shadow | enforce
        "backend": "llm",           // llm | jev
        "model": "",                // llm: bare chat model name; jev: decisions slug
        "api_key": "",              // jev only; env OPENROUTER_API_KEY fallback
        "base_url": "",             // jev only; "" → default decisions endpoint
        "timeout_s": 8,
        "router": {"enabled": true, "candidates": [], "min_confidence": 0.7},
        "tools":  {"enabled": true, "min_confidence": 0.6, "skip_below": 12,
                   "max_selected": 24},
        "shadow_log": "classifier_events.jsonl"   // relative to ~/.ginno; "" off
      }
    }

``GINNO_CLASSIFIER_MODE`` overrides ``mode`` for tests/ops (same pattern as
``GINNO_WF_PARALLEL``). Read per decision, so settings edits apply without a
session rebuild.
"""
import json
import os

from .. import paths

_DEFAULTS: dict = {
    "mode": "off",
    "backend": "llm",
    "model": "",
    # jev backend only: Decisions API key + endpoint. api_key falls back to
    # env OPENROUTER_API_KEY; base_url "" → the default decisions endpoint.
    "api_key": "",
    "base_url": "",
    "timeout_s": 8.0,
    "router": {
        "enabled": True,
        "candidates": [],  # empty = router middleware is INERT (grey-release safety valve)
        "min_confidence": 0.7,
    },
    "tools": {
        "enabled": True,
        "min_confidence": 0.6,
        "skip_below": 12,  # ≤ this many allowed tools → skip classification entirely
        "max_selected": 24,
    },
    "shadow_log": "classifier_events.jsonl",
}

_MODES = ("off", "shadow", "enforce")


def _merge(block: dict, defaults: dict) -> dict:
    out = dict(defaults)
    for k, v in block.items():
        if k not in defaults:
            continue  # unknown keys are dropped, same as context_settings
        if isinstance(v, dict) and isinstance(defaults[k], dict):
            out[k] = _merge(v, defaults[k])
        else:
            out[k] = v
    return out


def classifier_settings() -> dict:
    """Merged settings.classifier block (defaults ← user settings ← env)."""
    try:
        p = paths.settings_path()
        raw = json.loads(p.read_text() or "{}") if p.exists() else {}
    except Exception:  # noqa: BLE001 — settings must never break a turn
        raw = {}
    out = _merge(raw.get("classifier") or {}, _DEFAULTS)
    env = (os.environ.get("GINNO_CLASSIFIER_MODE") or "").strip().lower()
    if env in _MODES:
        out["mode"] = env
    if out["mode"] not in _MODES:
        out["mode"] = "off"
    return out
