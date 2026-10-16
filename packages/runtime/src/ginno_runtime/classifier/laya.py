# -*- coding: utf-8 -*-
"""Laya backend: generic laya decision-engine client over HTTP.

laya 是非自回归决策模型，HTTP 形态有两种部署方言，本客户端都支持——
``base_url`` 配**完整 endpoint URL**（路径含在内），按部署选择：

- **开源 laya-serve**（默认）：``http://127.0.0.1:8000/v1/systemone``，
  类 Jev wire 协议。无鉴权（api_key 可空）；服务端按输入语言自动路由
  checkpoint（中文 → multilingual）。
- **网关型部署**（如公司内网 AI 网关封装的 laya[serve]，路由形如
  ``/api/v0.1/predictions``）：Bearer token **必须**（401 = token 失效/
  为空）；请求体只有 ``state`` + ``questions``，无 model 字段，同样按
  语言自动路由 checkpoint。协议差异见下。

请求体只有 ``state`` + ``questions``——不发送 model 字段（两种方言都
接受省略，服务端自动路由）。题型 choice/noul/score 同请求混用；
**noul 一律不带 criteria**（网关方言带即 400 ``noul does not accept
criteria``，开源方言接受省略）；choice 的 criteria 是 ``{标签: 描述}``
（要求每个标签有描述）；score 的 criteria 是从低到高的等级数组。

与 jev.py 的关系：兄弟实现，刻意不共享代码（两个协议只是「今天同构」）。
题型映射沿用同一套展开——ScoreQuestion（逐项 0..1 语义，与 laya 的有序
等级 score 不同构）展开成每 item 一个 noul（服务端并行求值，不增加时延，
只加题）；per-item 语义塞进 instructions（noul 无 criteria 可用）。

confidence 语义：choice 的 ``confidence`` 是分布集中度（1−熵），与
middleware ``min_confidence`` 闸门同语义的是 ``answer_confidence``
（答案概率）。解析优先级：``answer_confidence`` → max(probs) → 0.0
——宁可 0.0 被判低置信走安全路径，也不拿集中度冒充答案概率。分布字段
两种方言分别叫 ``probs``（网关）与 ``probabilities``（laya-serve），
按存在性取用。

失败路径：服务不可达 / 401 / 503 busy（批队列满）/ 500 / 504 全部折成
ClassifierUnavailable → middleware 降级安全路径（60s 冷却，每 turn 一次
快速失败）。
"""
import asyncio
import logging
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

#: 默认 endpoint：本地开源 laya-serve。网关型部署把完整 URL（含路径）
#: 配进 settings ``classifier.base_url``，不做任何地址拼接/写死。
DEFAULT_ENDPOINT = "http://127.0.0.1:8000/v1/systemone"


def _question_payload(q: QuestionBase) -> dict:
    """Binary/Choice → the API's native shapes. ScoreQuestion is expanded
    per-item in classify() (noul per item), so it never reaches here."""
    if isinstance(q, ChoiceQuestion):
        return {
            "type": "choice",
            "instructions": q.prompt,
            # {标签: 描述}；文档要求每个标签有描述（描述帮助模型区分，
            # 也接受纯标签数组）。只有标签名时以标签自描述。
            "criteria": {o: o for o in q.options},
        }
    # noul：不带 criteria（网关方言带即 400），true/false 语义全靠
    # instructions。
    return {"type": "noul", "instructions": q.prompt}


class LayaSystemOneModel(ClassifierModelBase):
    """Generic laya HTTP client（laya-serve /v1/systemone 或网关型部署）."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        timeout_s: float = 15.0,
    ):
        self._api_key = api_key
        # base_url 语义 = 完整 endpoint URL（含路径），原样使用。
        self._url = (base_url or DEFAULT_ENDPOINT).rstrip("/")
        self._timeout_s = max(1.0, float(timeout_s))

    async def classify(
        self, state: dict, questions: list[QuestionBase]
    ) -> list[Answer]:
        from .llm import last_user_text

        if not self._api_key:
            # 网关必须鉴权：提前报配置错比放行后收 401 更可定位。
            raise ClassifierUnavailable(
                "laya backend not configured: set classifier.api_key "
                "(or env LAYA_API_KEY)"
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
                        # noul 无 criteria 可用，per-item 语义并进 instructions。
                        "instructions": (
                            f"{q.prompt} — is '{item}' clearly required "
                            "for handling this input?"
                        ),
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
                        # 无 model 字段：服务端按语言自动路由 checkpoint。
                        "state": last_user_text(state),
                        "questions": payload_questions,
                    }
                ),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            raise ClassifierUnavailable(
                f"laya predictions timed out after {self._timeout_s}s"
            ) from None
        except ClassifierUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — wrapped for the middleware
            raise ClassifierUnavailable(f"{type(e).__name__}: {e}") from None
        _log.debug(
            "laya predictions round-trip %.0fms",
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
                merged[q.id] = ChoiceAnswer(
                    question_id=q.id,
                    option=option,
                    confidence=self._answer_confidence(raw),
                )
            else:
                if rtype != "noul":
                    raise ClassifierUnavailable(f"answer {key!r}: expected noul")
                p = float(raw.get("noul") or 0.0)
                ac = raw.get("answer_confidence")
                confidence = (
                    max(0.0, min(1.0, float(ac)))
                    if isinstance(ac, (int, float))
                    else max(p, 1.0 - p)
                )
                merged[q.id] = BinaryAnswer(
                    question_id=q.id, value=p >= 0.5, confidence=confidence
                )
        return [merged[q.id] for q in questions]

    @staticmethod
    def _answer_confidence(raw: dict) -> float:
        """Laya 的 ``confidence`` 是 1−熵（分布集中度），与 Jev 的同名字段
        语义不同——min_confidence 闸门要的是「所报答案的概率」：优先
        ``answer_confidence``，缺失退分布最大值（网关方言叫 ``probs``，
        laya-serve 方言叫 ``probabilities``，按存在性取用），再缺 0.0。"""
        ac = raw.get("answer_confidence")
        if ac is None:
            probs = raw.get("probs") or raw.get("probabilities") or {}
            ac = max(probs.values()) if probs else None
        if ac is None:
            return 0.0
        try:
            return max(0.0, min(1.0, float(ac)))
        except (TypeError, ValueError):
            return 0.0

    async def _post(self, payload: dict) -> dict:
        """One HTTP round-trip. Split out so tests can stub the transport."""
        try:
            import httpx
        except ImportError as e:  # pragma: no cover — httpx rides every install
            raise ClassifierUnavailable(f"httpx unavailable: {e}") from None
        # trust_env=False：内网网关直连。dev 终端里全局 proxy env（如
        # all_proxy=socks5://127.0.0.1:7890）未配 NO_PROXY 时，httpx trust_env
        # 会在【构造期】就初始化代理传输——socks5 缺 socksio 直接 ImportError，
        # None mounts 都救不回来（挂载发生在代理初始化之后，2026-10-15 实测）。
        async with httpx.AsyncClient(
            timeout=self._timeout_s + 5, trust_env=False
        ) as client:
            r = await client.post(
                self._url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        if r.status_code != 200:
            # 401（token 失效）/ 503（批队列满）/ 500 / 504 / 4xx 校验错
            # 一律降级；body 片段进日志/异常消息供排障（文档 §七）。
            raise ClassifierUnavailable(
                f"laya api HTTP {r.status_code}: {r.text[:200]}"
            )
        return r.json()
