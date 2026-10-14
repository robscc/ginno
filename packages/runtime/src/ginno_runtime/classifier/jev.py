# -*- coding: utf-8 -*-
"""Jev (TypeSafe System One) backend via the native Decisions API.

OpenRouter 把 Jev 暴露在 ``POST /api/alpha/decisions``（不是 chat
completions）：请求体就是 ``{model, state, questions}``，题型 noul/choice/
score 与本包 schema 一一对应——Binary→noul、Choice→choice；Score 语义不同
（API 的 score 是"有序 rubric 上的位置"，本包是"逐项 0..1"），所以逐项
打分展开成每 item 一个 noul 题（API 对同一请求内的所有问题并行求值，
展开不增加时延，只加题）。

不带 openai-compatible 中转：不走 build_model / chat completions，直接
httpx POST。API key 取 settings ``classifier.api_key``，回落 env
``OPENROUTER_API_KEY``。
"""
import asyncio
import logging
import os
import time

from .base import ClassifierModelBase, ClassifierUnavailable
from .questions import (
    Answer,
    BinaryAnswer,
    ChoiceAnswer,
    ChoiceQuestion,
    QuestionBase,
    ScoreAnswer,
    ScoreQuestion,
)

_log = logging.getLogger("ginno.classifier")

DEFAULT_BASE_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"


def _question_payload(q: QuestionBase) -> dict:
    """Binary/Choice → the API's native shapes. ScoreQuestion is expanded
    per-item in classify() (noul per item), so it never reaches here."""
    if isinstance(q, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": q.prompt,
            # option names only; the API tolerates empty descriptions
            "criteria": {o: "" for o in q.options},
        }
    return {
        "type": "noul",
        "instructions": q.prompt,
        "criteria": {"true": "yes", "false": "no"},
    }


class JevDecisionsModel(ClassifierModelBase):
    """Native Decisions-API client — the closest mapping to Jev's own shape."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout_s: float = 15.0,
    ):
        self._api_key = api_key
        self._url = base_url or DEFAULT_BASE_URL
        self._model = model or DEFAULT_MODEL
        self._timeout_s = max(1.0, float(timeout_s))

    async def classify(
        self, state: dict, questions: list[QuestionBase]
    ) -> list[Answer]:
        from .llm import last_user_text

        if not self._api_key:
            raise ClassifierUnavailable(
                "jev backend not configured: set classifier.api_key "
                "(or env OPENROUTER_API_KEY)"
            )
        # 扁平展开：ScoreQuestion → 每 item 一个 noul（并行求值）。
        payload_questions: dict[str, dict] = {}
        plan: list[tuple[QuestionBase, str, str | None]] = []  # (question, key, item)
        for q in questions:
            if isinstance(q, ScoreQuestion):
                for item in q.items:
                    key = f"{q.id}::{item}"
                    payload_questions[key] = {
                        "type": "noul",
                        "instructions": q.prompt,
                        "criteria": {
                            "true": f"'{item}' is required",
                            "false": f"'{item}' is unrelated",
                        },
                    }
                    plan.append((q, key, item))
            else:
                payload_questions[q.id] = _question_payload(q)
                plan.append((q, q.id, None))

        started = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._post(
                    {
                        "model": self._model,
                        "state": last_user_text(state),
                        "questions": payload_questions,
                    }
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            raise ClassifierUnavailable(
                f"jev decisions api timed out after {self._timeout_s}s"
            ) from None
        except ClassifierUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — wrapped for the middleware
            raise ClassifierUnavailable(f"{type(e).__name__}: {e}") from None
        _log.debug(
            "jev decisions round-trip %.0fms",
            (time.perf_counter() - started) * 1000,
        )

        answers = resp.get("answers") or {}
        # 展开 per-item noul 的结果要合并回每题一个 Answer（契约：答案与
        # questions 1:1，middleware 的 _decide 只按 answers[i] 取第 i 题）。
        merged: dict[str, Answer] = {}
        for q, key, item in plan:
            raw = answers.get(key)
            if not isinstance(raw, dict):
                raise ClassifierUnavailable(f"missing answer for question {key!r}")
            rtype = raw.get("type")
            if isinstance(q, ScoreQuestion):
                if rtype != "noul":
                    raise ClassifierUnavailable(f"answer {key!r}: expected noul")
                score = max(0.0, min(1.0, float(raw.get("noul") or 0.0)))
                prev = merged.get(q.id)
                scores = dict(prev.scores) if isinstance(prev, ScoreAnswer) else {}
                scores[item] = score
                merged[q.id] = ScoreAnswer(question_id=q.id, scores=scores)
            elif isinstance(q, ChoiceQuestion):
                if rtype != "choice":
                    raise ClassifierUnavailable(f"answer {key!r}: expected choice")
                option = str(raw.get("choice") or "")
                if option not in q.options:
                    lowered = {o.lower(): o for o in q.options}
                    option = lowered.get(option.lower(), option)
                if option not in q.options:
                    raise ClassifierUnavailable(
                        f"answer {key!r}: {option!r} is not an allowed option"
                    )
                probs = {
                    str(k): float(v)
                    for k, v in (raw.get("probabilities") or {}).items()
                }
                confidence = raw.get("confidence")
                if confidence is None and probs:
                    confidence = max(probs.values())
                merged[q.id] = ChoiceAnswer(
                    question_id=q.id,
                    option=option,
                    confidence=max(0.0, min(1.0, float(confidence or 0.0))),
                )
            else:
                if rtype != "noul":
                    raise ClassifierUnavailable(f"answer {key!r}: expected noul")
                p = float(raw.get("noul") or 0.0)
                merged[q.id] = BinaryAnswer(
                    question_id=q.id, value=p >= 0.5,
                    confidence=p if p >= 0.5 else 1.0 - p,
                )
        return [merged[q.id] for q in questions]

    async def _post(self, payload: dict) -> dict:
        """One HTTP round-trip. Split out so tests can stub the transport."""
        try:
            import httpx
        except ImportError as e:  # pragma: no cover — httpx rides every install
            raise ClassifierUnavailable(f"httpx unavailable: {e}") from None
        async with httpx.AsyncClient(timeout=self._timeout_s + 5) as client:
            r = await client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        if r.status_code != 200:
            raise ClassifierUnavailable(
                f"decisions api HTTP {r.status_code}: {r.text[:200]}"
            )
        return r.json()
