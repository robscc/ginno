# -*- coding: utf-8 -*-
"""Graph-facing classifier middlewares: model routing + tool selection.

设计参照 AgentScope 的 ReAct middleware 模式（选择模型 / 选择工具 / 判断
可以吗），落到 Ginno 的 ``agent_node``：route 在 ``bind_tools``/``ainvoke``
之前替换模型实例，select 在 ``tool_allowed`` 之后收窄工具列表。铁律：

- **middleware 永不向 graph 抛异常** —— backend 失败/超时/低置信度一律降级
  到安全路径（原模型 / 全量工具），并记一条 degraded 事件。
- **OFF/SHADOW/ENFORCE 三档**：SHADOW 只记录建议（applied=false），ENFORCE
  才真正接管；置信度不足同样走安全路径。
- **按 turn 缓存**：同一用户消息（最后一条 HumanMessage）的多次 agent
  superstep 只分类一次；steer 注入的新 HumanMessage 换 key → 自然重分类。
  降级结果同样入缓存 + backend 失败 60s 冷却，坏 backend 的代价是每 turn
  一次快速失败，而不是每 superstep 一次超时。

不做 MiddlewareBase 泛型抽象：只有两个实现、签名不同，共享件（缓存/冷却/
shadow 记录）收在私有基类里即可。
"""
import hashlib
import logging
import os
import time
from pathlib import Path

from ginno_runtime import jsonx as json

from .. import paths
from ..tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ..tools.ask_tools import ASK_TOOL_NAMES
from ..tools.render_tools import RENDER_TOOL_NAMES
from ..tools.workflow_tools import WORKFLOW_TOOL_NAMES
from .base import ClassifierModelBase, ClassifierUnavailable
from .config import classifier_settings
from .llm import last_user_text
from .questions import Answer, ChoiceQuestion, ScoreQuestion

_log = logging.getLogger("ginno.classifier")

# 与 tool_allowed 的"永放行"集合对齐（graph.py）——工具筛选绝不切断这些
# 工具，否则 use_skill 懒加载链路和结构化输出会被误伤。
ALWAYS_ALLOW_TOOLS: frozenset[str] = frozenset(
    {*RENDER_TOOL_NAMES, *WORKFLOW_TOOL_NAMES, *ARTIFACT_TOOL_NAMES,
     *ASK_TOOL_NAMES, "use_skill"}
)

_COOLDOWN_S = 60.0
_LOG_ROTATE_BYTES = 10 * 1024 * 1024

# ENFORCE 路由选中的模型实例按名字缓存——build_model_by_name 只做构造，
# 但没必要每 superstep 重复实例化。
_MODEL_CACHE: dict[str, object] = {}


def _model_by_name_cached(name: str):
    if name not in _MODEL_CACHE:
        try:
            from .. import models as models_mod

            _MODEL_CACHE[name] = models_mod.build_model_by_name(name)
        except Exception as e:  # noqa: BLE001 — bad candidate → degrade, not crash
            _log.warning("classifier router: candidate model %r unavailable: %s", name, e)
            _MODEL_CACHE[name] = None
    return _MODEL_CACHE[name]


class _ClassifierMiddleware:
    """Shared plumbing: turn cache, failure cooldown, shadow-event log."""

    kind = "base"

    def __init__(self, backend: ClassifierModelBase, session_id: str):
        self.backend = backend
        self._session_id = session_id or ""
        self._cache_key: str | None = None
        self._cache_val: tuple[Answer | None, str] | None = None
        self._cooldown_until = 0.0

    # -- turn cache ---------------------------------------------------------

    @staticmethod
    def _turn_key(state: dict) -> str:
        """Identity of the current user input: the last HumanMessage's id
        (content hash when ids are absent). Steer absorption enqueues a NEW
        HumanMessage, so a new key — and a fresh classification — is exactly
        right there."""
        from langchain_core.messages import HumanMessage

        mid = None
        for m in reversed(state.get("messages", []) or []):
            if isinstance(m, HumanMessage):
                mid = getattr(m, "id", None)
                break
        if mid:
            return str(mid)
        return hashlib.sha1(last_user_text(state).encode()).hexdigest()[:16]

    async def _decide(
        self, state: dict, question
    ) -> tuple[Answer | None, str]:
        """Cache-aware single-question classify. Never raises; a backend
        failure is cached + cooled down and reported as ``(None, reason)``."""
        key = self._turn_key(state)
        if self._cache_key == key and self._cache_val is not None:
            return self._cache_val
        if time.time() < self._cooldown_until:
            val: tuple[Answer | None, str] = (None, "cooldown")
        else:
            try:
                answers = await self.backend.classify(state, [question])
                val = (answers[0], "")
            except ClassifierUnavailable as e:
                self._cooldown_until = time.time() + _COOLDOWN_S
                _log.warning("classifier %s degraded: %s", self.kind, e)
                val = (None, str(e))
        self._cache_key = key
        self._cache_val = val
        return val

    # -- shadow log ---------------------------------------------------------

    def _record(self, event: dict) -> None:
        """Append one decision to the shadow jsonl. Best-effort: recording
        must never affect the turn."""
        try:
            rel = classifier_settings().get("shadow_log")
            if not rel:
                return
            p = paths.home() / str(rel)
            try:
                if p.exists() and p.stat().st_size > _LOG_ROTATE_BYTES:
                    p.rename(p.with_name(p.name + ".1"))
            except OSError:
                pass
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "kind": self.kind,
                "session_id": self._session_id,
                **event,
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass


class ModelRouterMiddleware(_ClassifierMiddleware):
    """Pick the answering model per user input (Choice over ``candidates``).

    ``candidates`` 为空时本中间件惰性（安全阀）：ENFORCE 配错也不会把会话
    打到坏模型上。路由替换发生在 bind_tools/_system_message 之前，provider
    特性判断自动作用于新实例（graph.py agent_node_factory docstring 契约）。
    """

    kind = "router"

    async def route(self, model, state: dict, config: dict | None = None):
        cfg = classifier_settings()
        rcfg = cfg["router"]
        if cfg["mode"] == "off" or not rcfg["enabled"] or not rcfg["candidates"]:
            return model
        started = time.perf_counter()
        candidates = [str(c) for c in rcfg["candidates"]]
        question = ChoiceQuestion(
            id="model_route",
            prompt="Which model is best suited to answer this request? "
            "Pick the cheapest capable one.",
            options=candidates,
        )
        ans, reason = await self._decide(state, question)
        mode = cfg["mode"]
        applied = False
        if mode == "enforce" and ans is not None:
            if ans.confidence < rcfg["min_confidence"]:
                reason = f"low_confidence({ans.confidence:.2f})"
            else:
                picked = _model_by_name_cached(ans.option)
                if picked is not None:
                    model, applied = picked, True
                else:
                    reason = "candidate_model_unavailable"
        self._record({
            "mode": mode,
            "decision": ans.option if ans is not None else "",
            "confidence": round(ans.confidence, 3) if ans is not None else None,
            "applied": applied,
            "degraded": ans is None,
            "reason": reason,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "candidates_n": len(candidates),
        })
        return model


class ToolSelectionMiddleware(_ClassifierMiddleware):
    """Narrow the bound toolset per user input (Score over tool names).

    ENFORCE 且确实筛掉了工具时，graph 侧会把实际绑定清单注解到本 turn 的
    尾部 HumanMessage（copy-only，prefix cache 不受影响）。永放行集永远
    保留（与 ``tool_allowed`` 对齐）。
    """

    kind = "tools"

    async def select(self, state: dict, allowed: list):
        cfg = classifier_settings()
        tcfg = cfg["tools"]
        mode = cfg["mode"]
        if mode == "off" or not tcfg["enabled"] or len(allowed) <= tcfg["skip_below"]:
            return allowed
        started = time.perf_counter()
        names = [t.name for t in allowed]
        question = ScoreQuestion(
            id="tool_select",
            prompt="Score how likely each tool is needed to handle this "
            "input (0 = unrelated, 1 = clearly required).",
            items=names,
        )
        ans, reason = await self._decide(state, question)
        kept = allowed
        removed: list[str] = []
        if ans is None:
            pass  # degraded → full list
        elif mode != "enforce":
            reason = reason or "shadow"
        else:
            min_c = tcfg["min_confidence"]
            ranked = sorted(
                ((n, ans.scores.get(n, 0.0)) for n in names),
                key=lambda x: x[1],
                reverse=True,
            )
            top = {n for n, s in ranked if s >= min_c}
            # cap at max_selected by score, then restore the original order
            keep = {n for n, _ in ranked[: tcfg["max_selected"]]} & top
            keep |= ALWAYS_ALLOW_TOOLS
            kept = [t for t in allowed if t.name in keep]
            removed = [n for n in names if n not in keep]
            if not kept:  # paranoia: never bind an empty toolset
                kept, removed, reason = allowed, [], "all_scores_below_threshold"
        self._record({
            "mode": mode,
            "decision": f"{len(kept)}/{len(allowed)}",
            "removed": removed,
            "degraded": ans is None,
            "reason": reason,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "tools_n": len(allowed),
        })
        return kept


def build_middlewares(session_id: str):
    """Assemble ``(router, tool_selector)`` for one session's graph.

    mode=off → ``(None, None)``（零开销直通）。会话构建方在 4 处
    build_graph 调用点调用本函数；设置改动需重建会话图生效（运行中的
    SHADOW/ENFORCE 会话不受 settings 影响是可接受的边界）。
    """
    cfg = classifier_settings()
    if cfg["mode"] == "off":
        return None, None
    backend_name = str(cfg["backend"] or "llm").lower()
    if backend_name == "llm":
        from .llm import LlmClassifierModel

        backend = LlmClassifierModel(model_name=cfg["model"], timeout_s=cfg["timeout_s"])
    elif backend_name == "jev":
        from .jev import JevDecisionsModel

        backend = JevDecisionsModel(
            api_key=cfg["api_key"] or os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=cfg["base_url"],
            model=cfg["model"],
            timeout_s=cfg["timeout_s"],
        )
    elif backend_name == "laya":
        # SF AI 网关私有部署 laya 的 HTTP 客户端（laya.py 模块 docstring：
        # 协议要点、confidence 语义差异、失败降级路径）。网关自动路由
        # checkpoint，无 model 字段；Bearer token 必须。
        from .laya import LayaSystemOneModel

        backend = LayaSystemOneModel(
            api_key=cfg["api_key"] or os.environ.get("LAYA_API_KEY", ""),
            base_url=cfg["base_url"],
            timeout_s=cfg["timeout_s"],
        )
    else:
        _log.warning("classifier: unknown backend %r, staying off", backend_name)
        return None, None
    return (
        ModelRouterMiddleware(backend, session_id),
        ToolSelectionMiddleware(backend, session_id),
    )
