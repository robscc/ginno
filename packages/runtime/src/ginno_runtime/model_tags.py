"""Saved per-config model capabilities and catalogue recommendations."""

from __future__ import annotations

from typing import Any

BASE_EFFORTS = ("low", "medium", "high")
EXTENDED_EFFORTS = ("xhigh", "max", "ultra")
LEGACY_EFFORTS = ("off", *BASE_EFFORTS)
_ANTHROPIC_FIELDS = ("output_config", "reasoning_effort")


def codex_tag(cfg: dict | None, model: str | None) -> dict | None:
    """Look up a saved tag by the full model ID within this API config."""
    tags = (cfg or {}).get("model_tags")
    tag = tags.get(model) if isinstance(tags, dict) else None
    if isinstance(tag, dict) and tag.get("tag") == "codex":
        return tag
    return None


def normalize_model_tags(cfg: dict, models: list[str]) -> dict[str, dict]:
    """Canonicalize tags and drop entries for models outside the config."""
    tags = {}
    for model in models:
        tag = codex_tag(cfg, model)
        if tag is None:
            continue
        entry = {"tag": "codex"}
        if tag.get("max_effort") in EXTENDED_EFFORTS:
            entry["max_effort"] = tag["max_effort"]
        if tag.get("anthropic_effort_field") in _ANTHROPIC_FIELDS:
            entry["anthropic_effort_field"] = tag["anthropic_effort_field"]
        tags[model] = entry
    return tags


def codex_efforts(cfg: dict | None, model: str | None) -> tuple[str, ...] | None:
    """None means default; an empty tuple means Codex with an unknown cap."""
    tag = codex_tag(cfg, model)
    if tag is None:
        return None
    cap = tag.get("max_effort")
    if cap not in EXTENDED_EFFORTS:
        return ()
    return BASE_EFFORTS + EXTENDED_EFFORTS[:EXTENDED_EFFORTS.index(cap) + 1]


def resolve_model_effort(
    cfg: dict | None, model: str | None, value: Any, *, strict: bool = False,
) -> str | None:
    """Resolve an inherited preference or validate an explicit choice."""
    raw = str(value or "").strip().lower()
    if raw in ("", "auto"):
        return None
    codex = codex_efforts(cfg, model)
    options = LEGACY_EFFORTS if codex is None else codex
    if raw == "off" and codex and not strict:
        return "low"
    if raw in options:
        return raw
    # Default models have historically treated unknown strings as auto.
    if strict and (codex is not None or raw in EXTENDED_EFFORTS):
        available = ", ".join(("auto", *options))
        raise ValueError(f"Unsupported thinking effort {raw!r}. Available: {available}")
    return None


def inherit_model_effort(
    source_cfg: dict | None, source_model: str | None,
    target_cfg: dict | None, target_model: str | None, value: Any,
) -> str | None:
    """Carry an already usable effort across a model or capability change."""
    source = codex_tag(source_cfg, source_model)
    if source is not None and codex_tag(target_cfg, target_model) is None:
        return None
    value = resolve_model_effort(source_cfg, source_model, value)
    return resolve_model_effort(target_cfg, target_model, value)


def catalogue_model_tag(row: dict) -> dict[str, str] | None:
    """Read an explicit Codex family declaration and its advertised levels."""
    capabilities = row.get("capabilities")
    for metadata in (capabilities, row):
        if not isinstance(metadata, dict):
            continue
        family = metadata.get("model_family") or metadata.get("family")
        if not isinstance(family, str) or family.strip().lower() != "codex":
            continue
        levels = metadata.get("supported_reasoning_efforts")
        if levels is None:
            levels = metadata.get("supported_reasoning_levels")
        if isinstance(levels, list):
            efforts = [level.get("effort") if isinstance(level, dict) else level for level in levels]
            for effort in reversed(EXTENDED_EFFORTS):
                if effort in efforts:
                    return {"tag": "codex", "max_effort": effort}
        return {"tag": "codex"}
    return None
