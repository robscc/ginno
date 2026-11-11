"""Instincts 数据模型与配置（docs/instincts-design.md §4/§10.1）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import paths

DOMAINS = ("tool-usage", "code-style", "workflow", "security", "user-preference")
STATUSES = ("pending", "active", "evolved", "pruned")
SOURCES = ("permission-deny", "user-correction", "retry-recover", "manual")


@dataclass
class InstinctsConfig:
    """settings.json `instincts` 段（§10.1；缺省全开但注入门槛 0.7）。"""

    enabled: bool = True
    capture: bool = True
    inject_enabled: bool = True
    inject_min_confidence: float = 0.7
    inject_max_n: int = 6
    inject_token_budget: int = 600
    template_threshold: int = 3  # 同类 tool_denied 观察达到该次数 → 生成 pending
    decay_days: int = 90
    # L2 observer（docs/instincts-design.md §6）：provider 空 = 禁用 LLM 判定
    # （纯模板模式）；model_id 空 = 供应商默认小模型。
    observer_model: str = ""
    observer_model_id: str = ""
    min_observations: int = 20  # 新观察达到该数才触发一次分析
    analyze_gap_min: int = 5    # 两次分析最小间隔（分钟）


def _read_settings() -> dict[str, Any]:
    p = paths.settings_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def load_instincts_config(settings: dict[str, Any] | None = None) -> InstinctsConfig:
    """从 settings 文档读 instincts 段（透传端点自动可见，无需改 settings API）。"""
    doc = settings if settings is not None else _read_settings()
    raw = doc.get("instincts") or {}
    cfg = InstinctsConfig()
    for k in ("enabled", "capture", "inject_enabled"):
        if isinstance(raw.get(k), bool):
            setattr(cfg, k, raw[k])
    for k in ("inject_min_confidence",):
        v = raw.get(k)
        if isinstance(v, (int, float)):
            setattr(cfg, k, float(v))
    for k in ("inject_max_n", "inject_token_budget", "template_threshold", "decay_days",
              "min_observations", "analyze_gap_min"):
        v = raw.get(k)
        if isinstance(v, int) and v > 0:
            setattr(cfg, k, v)
    for k in ("observer_model", "observer_model_id"):
        v = raw.get(k)
        if isinstance(v, str):
            setattr(cfg, k, v.strip())
    return cfg


@dataclass
class Instinct:
    """一条原子行为规则：trigger（何时适用）+ behavior（该做什么）+ 置信度。

    YAML 一文件一条（id 即文件名）；字段语义见设计文档 §4.1。
    """

    id: str
    trigger: str
    behavior: str
    confidence: float = 0.3
    domain: str = "tool-usage"
    scope: str = "project"  # project | global
    project_slug: str = ""
    source: str = "manual"
    status: str = "pending"
    evidence: dict[str, Any] = field(
        default_factory=lambda: {
            "observed": 0,
            "applied": 0,
            "uncorrected": 0,
            "corrected": 0,
            "last_seen": "",
        }
    )
    created: str = ""
    updated: str = ""
    evolved_to: str | None = None

    def to_yaml_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evolved_to"] = self.evolved_to
        return d

    @staticmethod
    def from_yaml_dict(d: dict[str, Any]) -> "Instinct":
        known = {f for f in Instinct.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in d.items() if k in known}
        ev = clean.get("evidence") or {}
        clean["evidence"] = {
            "observed": int(ev.get("observed", 0)),
            "applied": int(ev.get("applied", 0)),
            "uncorrected": int(ev.get("uncorrected", 0)),
            "corrected": int(ev.get("corrected", 0)),
            "last_seen": str(ev.get("last_seen", "")),
        }
        return Instinct(**clean)
