# -*- coding: utf-8 -*-
"""Default classifier backend: any configured chat model + structured output.

build_model_by_name 只做构造（读 settings + 实例化），无网络 IO，所以可以
在 async 里同步调用（mods._build_model 同款先例）。structured output 走
``with_structured_output``（include_raw 保留 usage）；某些 openai-compatible
网关不支持 tool-calling 式 structured output，因此文本 JSON 抽取是一等降级
路径，不是错误处理。
"""
import asyncio
import logging
import re
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ginno_runtime import jsonx as json

from .. import usage as usage_mod
from .. import usage_store
from .base import ClassifierModelBase, ClassifierUnavailable
from .questions import (
    Answer,
    BinaryAnswer,
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifierReply,
    QuestionBase,
    ScoreAnswer,
    ScoreQuestion,
)

_log = logging.getLogger("ginno.classifier")

_SYSTEM = (
    "You are a strict classifier. Answer every question exactly as "
    "instructed. Never explain, never add fields."
)


def last_user_text(state: dict) -> str:
    """The text of the newest HumanMessage in the graph state — the classifier's
    shared input. Multimodal content contributes its text blocks only."""
    from langchain_core.messages import HumanMessage

    for m in reversed(state.get("messages", []) or []):
        if isinstance(m, HumanMessage):
            content = m.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    return joined
    return ""


def _render_questions(questions: list[QuestionBase]) -> str:
    lines: list[str] = []
    for i, q in enumerate(questions, 1):
        if isinstance(q, ChoiceQuestion):
            lines.append(
                f"{i}. id={q.id} (choice): {q.prompt}\n"
                f"   Options: {', '.join(q.options)}"
            )
        elif isinstance(q, ScoreQuestion):
            lines.append(
                f"{i}. id={q.id} (score): {q.prompt}\n"
                f"   Items: {', '.join(q.items)}"
            )
        else:
            lines.append(f"{i}. id={q.id} (binary): {q.prompt} Answer true or false.")
    return "\n".join(lines)


def _user_prompt(state_text: str, questions: list[QuestionBase]) -> str:
    return (
        f"Input:\n{state_text or '(empty)'}\n\n"
        f"Questions:\n{_render_questions(questions)}\n\n"
        'Reply with JSON only: {"answers": [{"question_id": "...", '
        '"type": "choice", "option": "...", "confidence": 0.0-1.0, ...}]} '
        "(choice answers carry option+confidence, score answers carry "
        "scores{item: 0.0-1.0}, binary answers carry value+confidence)."
    )


def _extract_json(text: str) -> dict:
    """First balanced JSON object in a (possibly chatty) reply."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unbalanced JSON in reply")


_STRUCTURED_BREAK_RE = re.compile(
    r"structured|schema|response_format|tool_choice|400|404|422|unsupported",
    re.IGNORECASE,
)


class LlmClassifierModel(ClassifierModelBase):
    """classify() over any configured provider via structured output."""

    def __init__(self, model_name: str = "", timeout_s: float = 8.0):
        self._model_name = (model_name or "").strip()
        self._timeout_s = max(1.0, float(timeout_s))
        self._model = None
        self._structured = None
        self._prefer_text = False

    def _ensure_model(self):
        if self._model is None:
            from .. import models as models_mod

            self._model = models_mod.build_model_by_name(self._model_name)
        return self._model

    def _ensure_structured(self):
        if self._structured is None:
            self._structured = self._ensure_model().with_structured_output(
                ClassifierReply, include_raw=True
            )
        return self._structured

    def _record_usage(self, raw: Any, latency_ms: int, ok: bool, error: str = "") -> None:
        try:
            u = usage_mod.extract_usage(raw) or {} if raw is not None else {}
            usage_store.record(
                input_tokens=u.get("input_tokens") or 0,
                output_tokens=u.get("output_tokens") or 0,
                cache_read_tokens=u.get("cache_read_tokens") or 0,
                cache_creation_tokens=u.get("cache_creation_tokens") or 0,
                provider="",
                model=self._model_name or "default",
                source="classifier",
                latency_ms=latency_ms,
                ok=ok,
                error=(error or None) if not ok else None,
            )
        except Exception:  # noqa: BLE001 — telemetry must never break a turn
            pass

    async def classify(
        self, state: dict, questions: list[QuestionBase]
    ) -> list[Answer]:
        started = time.perf_counter()
        prompt = _user_prompt(last_user_text(state), questions)
        messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=prompt)]
        raw = None
        try:
            if self._prefer_text:
                reply = await self._classify_text(messages)
            else:
                try:
                    structured = self._ensure_structured()
                    res = await asyncio.wait_for(
                        structured.ainvoke(messages), timeout=self._timeout_s
                    )
                    raw = res.get("raw")
                    if res.get("parsing_error"):
                        raise ValueError(str(res["parsing_error"])[:200])
                    reply = res["parsed"]
                except asyncio.TimeoutError:
                    raise
                except Exception as e:  # noqa: BLE001 — try the text path once
                    if type(e) in (TypeError, NotImplementedError) or (
                        _STRUCTURED_BREAK_RE.search(f"{type(e).__name__}: {e}")
                    ):
                        # Provider likely can't do schema-constrained output —
                        # stop paying for the failed attempt on every call.
                        self._prefer_text = True
                        self._structured = None
                        _log.info("classifier structured output unavailable, using text path")
                        reply = await self._classify_text(messages)
                    else:
                        raise
        except asyncio.TimeoutError:
            self._record_usage(raw, int((time.perf_counter() - started) * 1000),
                               ok=False, error="timeout")
            raise ClassifierUnavailable(
                f"classifier timed out after {self._timeout_s}s"
            ) from None
        except Exception as e:  # noqa: BLE001 — wrapped for the middleware
            self._record_usage(raw, int((time.perf_counter() - started) * 1000),
                               ok=False, error=f"{type(e).__name__}: {e}"[:300])
            raise ClassifierUnavailable(f"{type(e).__name__}: {e}") from None
        self._record_usage(raw, int((time.perf_counter() - started) * 1000), ok=True)
        return self._validate(reply, questions)

    async def _classify_text(self, messages: list) -> ClassifierReply:
        model = self._ensure_model()
        resp = await asyncio.wait_for(model.ainvoke(messages), timeout=self._timeout_s)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        return ClassifierReply.model_validate(_extract_json(text))

    def _validate(
        self, reply: ClassifierReply, questions: list[QuestionBase]
    ) -> list[Answer]:
        """Align answers with questions; all-or-nothing (one bad answer →
        ClassifierUnavailable → the middleware degrades to the safe path)."""
        by_id = {a.question_id: a for a in reply.answers}
        out: list[Answer] = []
        for q in questions:
            a = by_id.get(q.id)
            if a is None:
                raise ClassifierUnavailable(f"missing answer for question {q.id!r}")
            if isinstance(q, ChoiceQuestion):
                if not isinstance(a, ChoiceAnswer):
                    raise ClassifierUnavailable(f"answer {q.id!r}: expected choice")
                if a.option not in q.options:
                    lowered = {o.lower(): o for o in q.options}
                    hit = lowered.get(a.option.lower())
                    if hit is None:
                        raise ClassifierUnavailable(
                            f"answer {q.id!r}: {a.option!r} is not an allowed option"
                        )
                    a = a.model_copy(update={"option": hit})
                out.append(a)
            elif isinstance(q, ScoreQuestion):
                if not isinstance(a, ScoreAnswer):
                    raise ClassifierUnavailable(f"answer {q.id!r}: expected score")
                scores = {
                    k: max(0.0, min(1.0, float(v))) for k, v in a.scores.items()
                }
                out.append(a.model_copy(update={"scores": scores}))
            else:
                if not isinstance(a, BinaryAnswer):
                    raise ClassifierUnavailable(f"answer {q.id!r}: expected binary")
                out.append(a)
        return out
