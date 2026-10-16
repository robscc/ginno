"""Unit tests for the classifier middleware layer (no graph): question/answer
schema, settings merge + env override, the LLM backend's structured-output and
text-fallback paths, and both middlewares' OFF/SHADOW/ENFORCE + degrade +
turn-cache semantics."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime import paths
from ginno_runtime.classifier.base import ClassifierModelBase, ClassifierUnavailable
from ginno_runtime.classifier.config import classifier_settings
from ginno_runtime.classifier.llm import LlmClassifierModel
from ginno_runtime.classifier.middlewares import (
    ALWAYS_ALLOW_TOOLS,
    ModelRouterMiddleware,
    ToolSelectionMiddleware,
    build_middlewares,
)
from ginno_runtime.classifier.questions import (
    BinaryAnswer,
    BinaryQuestion,
    ChoiceAnswer,
    ChoiceQuestion,
    ClassifierReply,
    ScoreAnswer,
    ScoreQuestion,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _set_settings(block: dict, isolated_home) -> None:
    p = paths.settings_path()
    raw = json.loads(p.read_text()) if p.exists() else {}
    raw.update(block)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw))


def _state(text: str = "hello", mid: str = "u1") -> dict:
    return {"messages": [HumanMessage(id=mid, content=text)]}


class FakeBackend(ClassifierModelBase):
    """Scripted backend: replays answers, counts calls, optional failure."""

    def __init__(self, answers=None, exc=None):
        self.answers = list(answers or [])
        self.exc = exc
        self.calls: list[list[str]] = []

    async def classify(self, state, questions):
        self.calls.append([q.id for q in questions])
        if self.exc is not None:
            raise ClassifierUnavailable(self.exc)
        return list(self.answers)


def _choice(option: str, confidence: float = 0.9, qid: str = "model_route"):
    return ChoiceAnswer(question_id=qid, option=option, confidence=confidence)


@pytest.fixture(autouse=True)
def _clear_model_cache(monkeypatch):
    monkeypatch.setattr(
        "ginno_runtime.classifier.middlewares._MODEL_CACHE", {}
    )


# --------------------------------------------------------------------------- #
# question / answer schema
# --------------------------------------------------------------------------- #
def test_reply_roundtrip_discriminated_union():
    reply = ClassifierReply.model_validate(
        {
            "answers": [
                {"question_id": "b", "type": "binary", "value": True, "confidence": 0.8},
                {"question_id": "c", "type": "choice", "option": "fast", "confidence": 0.9},
                {"question_id": "s", "type": "score", "scores": {"a": 0.5}},
            ]
        }
    )
    kinds = [a.type for a in reply.answers]
    assert kinds == ["binary", "choice", "score"]


def test_reply_rejects_unknown_fields():
    with pytest.raises(Exception):
        ClassifierReply.model_validate(
            {"answers": [{"question_id": "x", "type": "binary", "value": 1, "nope": 2}]}
        )


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def test_settings_defaults_off(isolated_home):
    assert classifier_settings()["mode"] == "off"
    assert classifier_settings()["router"]["candidates"] == []
    assert classifier_settings()["shadow_log"] == "classifier_events.jsonl"


def test_settings_merge_filters_unknown_keys(isolated_home):
    _set_settings(
        {
            "classifier": {
                "mode": "enforce",
                "nope": 1,
                "router": {"candidates": ["m1"], "bogus": True},
                "timeout_s": 3,
            }
        },
        isolated_home,
    )
    s = classifier_settings()
    assert s["mode"] == "enforce"
    assert s["router"] == {
        "enabled": True, "candidates": ["m1"], "min_confidence": 0.7,
    }
    assert "nope" not in s and s["timeout_s"] == 3


def test_settings_env_mode_override(isolated_home, monkeypatch):
    _set_settings({"classifier": {"mode": "shadow"}}, isolated_home)
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "enforce")
    assert classifier_settings()["mode"] == "enforce"
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "garbage")
    assert classifier_settings()["mode"] == "shadow"  # env ignored, settings win


def test_build_middlewares_off_is_none(isolated_home):
    assert build_middlewares("s1") == (None, None)


# --------------------------------------------------------------------------- #
# LLM backend
# --------------------------------------------------------------------------- #
class _FakeStructured:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc

    async def ainvoke(self, messages):
        if self.exc is not None:
            raise self.exc
        return {"raw": None, "parsed": self.result, "parsing_error": None}


class _FakeLLM:
    def __init__(self, structured=None, text_reply=""):
        self._structured = structured
        self.text_reply = text_reply
        self.ainvoke_calls = 0

    def with_structured_output(self, schema, include_raw=False):
        return self._structured

    async def ainvoke(self, messages):
        self.ainvoke_calls += 1
        return AIMessage(content=self.text_reply)


def _patch_build_model(monkeypatch, llm):
    monkeypatch.setattr(
        "ginno_runtime.models.build_model_by_name", lambda name="": llm
    )


async def test_llm_backend_structured_success(isolated_home, monkeypatch):
    reply = ClassifierReply(answers=[_choice("fast")])
    llm = LlmClassifierModel(model_name="m", timeout_s=5)
    _patch_build_model(monkeypatch, _FakeLLM(structured=_FakeStructured(result=reply)))
    out = await llm.classify(_state(), [ChoiceQuestion(id="model_route", prompt="p", options=["fast", "slow"])])
    assert out[0].option == "fast"


async def test_llm_backend_structured_unsupported_falls_back_to_text(isolated_home, monkeypatch):
    text = json.dumps(
        ClassifierReply(answers=[_choice("slow")]).model_dump()
    )
    llm = _FakeLLM(
        structured=_FakeStructured(exc=TypeError("structured output unsupported")),
        text_reply=f"Sure! {text}",
    )
    _patch_build_model(monkeypatch, llm)
    model = LlmClassifierModel(model_name="m", timeout_s=5)
    qs = [ChoiceQuestion(id="model_route", prompt="p", options=["fast", "slow"])]
    out = await model.classify(_state(), qs)
    assert out[0].option == "slow"
    assert model._prefer_text is True
    # subsequent calls skip the structured attempt entirely
    await model.classify(_state(), qs)
    assert llm.ainvoke_calls == 2


async def test_llm_backend_timeout_raises_unavailable(isolated_home, monkeypatch):
    import asyncio

    class Slow:
        def with_structured_output(self, schema, include_raw=False):
            return self

        async def ainvoke(self, messages):
            await asyncio.sleep(1.5)
            return {}

    _patch_build_model(monkeypatch, Slow())
    model = LlmClassifierModel(model_name="m", timeout_s=1)
    with pytest.raises(ClassifierUnavailable, match="timed out"):
        await model.classify(
            _state(), [ChoiceQuestion(id="model_route", prompt="p", options=["a"])]
        )


async def test_llm_backend_invalid_option_is_unavailable(isolated_home, monkeypatch):
    reply = ClassifierReply(answers=[_choice("nonexistent")])
    _patch_build_model(
        monkeypatch, _FakeLLM(structured=_FakeStructured(result=reply))
    )
    model = LlmClassifierModel(model_name="m", timeout_s=5)
    with pytest.raises(ClassifierUnavailable, match="not an allowed option"):
        await model.classify(
            _state(),
            [ChoiceQuestion(id="model_route", prompt="p", options=["fast", "slow"])],
        )


async def test_jev_backend_without_key_is_unavailable(isolated_home, monkeypatch):
    from ginno_runtime.classifier.jev import JevDecisionsModel

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ClassifierUnavailable, match="not configured"):
        await JevDecisionsModel().classify(_state(), [])


async def test_jev_backend_parses_decisions_payload(isolated_home, monkeypatch):
    from ginno_runtime.classifier.jev import JevDecisionsModel

    async def fake_post(self, payload):
        assert payload["model"] == "~typesafe/jev-latest"
        qs = payload["questions"]
        # ScoreQuestion expands to one noul per item
        assert "tool_select::bash" in qs and "tool_select::render_widget" in qs
        assert qs["model_route"]["type"] == "choice"
        return {
            "answers": {
                "model_route": {"type": "choice", "choice": "claude-sonnet",
                                 "confidence": 0.95},
                "tool_select::bash": {"type": "noul", "noul": 0.97},
                "tool_select::render_widget": {"type": "noul", "noul": 0.1},
            }
        }

    monkeypatch.setattr(JevDecisionsModel, "_post", fake_post)
    model = JevDecisionsModel(api_key="k", model="~typesafe/jev-latest")
    out = await model.classify(
        _state(),
        [
            ChoiceQuestion(id="model_route", prompt="p", options=["qwen-flash", "claude-sonnet"]),
            ScoreQuestion(id="tool_select", prompt="p", items=["bash", "render_widget"]),
        ],
    )
    assert out[0].option == "claude-sonnet" and out[0].confidence == 0.95
    # two noul answers for the same ScoreQuestion merge into one ScoreAnswer
    assert out[1].scores == {"bash": 0.97, "render_widget": 0.1}


async def test_jev_backend_rejects_unknown_option(isolated_home, monkeypatch):
    from ginno_runtime.classifier.jev import JevDecisionsModel

    async def fake_post(self, payload):
        return {"answers": {"q": {"type": "choice", "choice": "bogus", "confidence": 0.9}}}

    monkeypatch.setattr(JevDecisionsModel, "_post", fake_post)
    model = JevDecisionsModel(api_key="k")
    with pytest.raises(ClassifierUnavailable, match="not an allowed option"):
        await model.classify(
            _state(), [ChoiceQuestion(id="q", prompt="p", options=["a", "b"])]
        )


# --------------------------------------------------------------------------- #
# Laya backend (SF AI gateway /api/v0.1/predictions)
# --------------------------------------------------------------------------- #
async def test_laya_backend_without_key_is_unavailable(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    with pytest.raises(ClassifierUnavailable, match="not configured"):
        await LayaSystemOneModel().classify(_state(), [])


async def test_laya_backend_parses_gateway_payload(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        # SF 网关契约：请求体只有 state + questions（无 model 字段——服务端
        # 按语言自动路由 checkpoint）。
        assert set(payload) == {"state", "questions"}
        qs = payload["questions"]
        # ScoreQuestion expands to one noul per item (never sends laya "score")
        assert "tool_select::bash" in qs and "tool_select::render_widget" in qs
        assert all(v["type"] in ("noul", "choice") for v in qs.values())
        # noul 不能带 criteria（带即 400，网关文档 §4.3）
        for v in qs.values():
            if v["type"] == "noul":
                assert "criteria" not in v
        # choice 的 criteria 是 {标签: 描述}，描述必须非空
        assert qs["model_route"]["criteria"] == {
            "qwen-flash": "qwen-flash", "claude-sonnet": "claude-sonnet",
        }
        return {
            "answers": {
                # Laya 的 confidence 是 1−熵，必须被忽略；answer_confidence
                # 才是答案概率（middleware 阈值同语义）。分布字段叫 probs。
                "model_route": {
                    "type": "choice", "choice": "claude-sonnet",
                    "confidence": 0.99, "answer_confidence": 0.72,
                    "probs": {"qwen-flash": 0.28, "claude-sonnet": 0.72},
                },
                "tool_select::bash": {"type": "noul", "noul": 0.97},
                "tool_select::render_widget": {"type": "noul", "noul": 0.1},
            }
        }

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    model = LayaSystemOneModel(api_key="k")
    out = await model.classify(
        _state(),
        [
            ChoiceQuestion(id="model_route", prompt="p", options=["qwen-flash", "claude-sonnet"]),
            ScoreQuestion(id="tool_select", prompt="p", items=["bash", "render_widget"]),
        ],
    )
    assert out[0].option == "claude-sonnet" and out[0].confidence == 0.72
    assert out[1].scores == {"bash": 0.97, "render_widget": 0.1}


async def test_laya_backend_choice_confidence_falls_back_to_probs(
    isolated_home, monkeypatch
):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        return {
            "answers": {
                "q": {
                    "type": "choice", "choice": "a", "confidence": 0.88,
                    "probs": {"a": 0.61, "b": 0.39},
                }
            }
        }

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    out = await LayaSystemOneModel(api_key="k").classify(
        _state(), [ChoiceQuestion(id="q", prompt="p", options=["a", "b"])]
    )
    assert out[0].confidence == 0.61  # max(probs), NOT the entropy field


async def test_laya_backend_choice_confidence_layaserve_dialect(
    isolated_home, monkeypatch
):
    """开源 laya-serve 方言：分布字段叫 probabilities（不是网关的 probs）。"""
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        return {
            "answers": {
                "q": {
                    "type": "choice", "choice": "b", "confidence": 0.9,
                    "probabilities": {"a": 0.2, "b": 0.8},
                }
            }
        }

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    out = await LayaSystemOneModel(api_key="k").classify(
        _state(), [ChoiceQuestion(id="q", prompt="p", options=["a", "b"])]
    )
    assert out[0].confidence == 0.8


async def test_laya_backend_choice_confidence_missing_is_zero(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        return {"answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.9}}}

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    out = await LayaSystemOneModel(api_key="k").classify(
        _state(), [ChoiceQuestion(id="q", prompt="p", options=["a", "b"])]
    )
    # 熵集中度不冒充答案概率 → 0.0，让 min_confidence 闸门走安全路径
    assert out[0].confidence == 0.0


async def test_laya_backend_noul_uses_answer_confidence(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        # 网关响应示例形状（文档 §五）：noul 也带 answer_confidence
        return {"answers": {"q": {"type": "noul", "noul": 0.0226,
                                  "confidence": 0.9774, "answer_confidence": 0.9774}}}

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    out = await LayaSystemOneModel(api_key="k").classify(
        _state(), [BinaryQuestion(id="q", prompt="p")]
    )
    assert out[0].value is False and out[0].confidence == 0.9774


async def test_laya_backend_rejects_unknown_option(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        return {
            "answers": {
                "q": {"type": "choice", "choice": "bogus", "answer_confidence": 0.9}
            }
        }

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    with pytest.raises(ClassifierUnavailable, match="not an allowed option"):
        await LayaSystemOneModel(api_key="k").classify(
            _state(), [ChoiceQuestion(id="q", prompt="p", options=["a", "b"])]
        )


async def test_laya_backend_connection_error_is_unavailable(isolated_home, monkeypatch):
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    async def fake_post(self, payload):
        raise OSError("connection refused")  # 网关不可达

    monkeypatch.setattr(LayaSystemOneModel, "_post", fake_post)
    with pytest.raises(ClassifierUnavailable, match="connection refused"):
        await LayaSystemOneModel(api_key="k").classify(
            _state(), [BinaryQuestion(id="q", prompt="p")]
        )


def test_laya_backend_url_composition(isolated_home):
    """base_url 语义 = 完整 endpoint URL（含路径），原样使用不拼接；
    默认是本地开源 laya-serve——公司网关等部署地址只进 settings 配置，
    不写死进代码。"""
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    assert LayaSystemOneModel(api_key="k")._url == "http://127.0.0.1:8000/v1/systemone"
    assert (
        LayaSystemOneModel(
            api_key="k", base_url="http://10.0.0.5:9001/svc/api/v0.1/predictions"
        )._url
        == "http://10.0.0.5:9001/svc/api/v0.1/predictions"
    )


def test_build_middlewares_laya_backend(isolated_home, monkeypatch):
    monkeypatch.delenv("LAYA_API_KEY", raising=False)
    _set_settings(
        {"classifier": {"backend": "laya", "api_key": "settings-key"}}, isolated_home
    )
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "shadow")
    router, selector = build_middlewares("s1")
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    assert isinstance(router.backend, LayaSystemOneModel)
    assert isinstance(selector.backend, LayaSystemOneModel)
    assert router.backend._api_key == "settings-key"


def test_build_middlewares_laya_env_key_fallback(isolated_home, monkeypatch):
    _set_settings({"classifier": {"backend": "laya"}}, isolated_home)
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "shadow")
    monkeypatch.setenv("LAYA_API_KEY", "env-key")
    router, _ = build_middlewares("s1")
    assert router.backend._api_key == "env-key"


# --------------------------------------------------------------------------- #
# ModelRouterMiddleware
# --------------------------------------------------------------------------- #
def _router_cfg(isolated_home, monkeypatch, mode, candidates=("model-a", "model-b")):
    _set_settings(
        {"classifier": {"router": {"candidates": list(candidates)}}}, isolated_home
    )
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", mode)


async def test_router_off_passthrough(isolated_home, monkeypatch):
    backend = FakeBackend()
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is sentinel
    assert backend.calls == []


async def test_router_shadow_records_but_keeps_model(isolated_home, monkeypatch):
    _router_cfg(isolated_home, monkeypatch, "shadow")
    backend = FakeBackend(answers=[_choice("model-b")])
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is sentinel
    log = paths.home() / "classifier_events.jsonl"
    events = [json.loads(l) for l in log.read_text().splitlines()]
    assert events[0]["kind"] == "router" and events[0]["mode"] == "shadow"
    assert events[0]["decision"] == "model-b" and events[0]["applied"] is False


async def test_router_enforce_applies_choice(isolated_home, monkeypatch):
    _router_cfg(isolated_home, monkeypatch, "enforce")
    picked = SimpleNamespace(name="picked-b")
    monkeypatch.setattr(
        "ginno_runtime.models.build_model_by_name", lambda name="": picked
    )
    backend = FakeBackend(answers=[_choice("model-b", 0.9)])
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is picked


async def test_router_enforce_low_confidence_degrades(isolated_home, monkeypatch):
    _router_cfg(isolated_home, monkeypatch, "enforce")
    backend = FakeBackend(answers=[_choice("model-b", 0.3)])
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is sentinel
    log = paths.home() / "classifier_events.jsonl"
    event = json.loads(log.read_text().splitlines()[-1])
    assert event["applied"] is False and "low_confidence" in event["reason"]


async def test_router_backend_failure_degrades_and_cools_down(isolated_home, monkeypatch):
    _router_cfg(isolated_home, monkeypatch, "enforce")
    backend = FakeBackend(exc="boom")
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is sentinel
    # cooldown: a NEW user message within 60s must not call the backend again
    backend.exc = None
    assert await mw.route(sentinel, _state(mid="u2")) is sentinel
    assert len(backend.calls) == 1
    log = paths.home() / "classifier_events.jsonl"
    event = json.loads(log.read_text().splitlines()[-1])
    assert event["degraded"] is True and event["reason"] == "cooldown"


async def test_router_turn_cache_hits_once_per_user_message(isolated_home, monkeypatch):
    _router_cfg(isolated_home, monkeypatch, "shadow")
    backend = FakeBackend(answers=[_choice("model-b")])
    mw = ModelRouterMiddleware(backend, "s1")
    await mw.route(object(), _state(mid="u1"))
    await mw.route(object(), _state(mid="u1"))
    assert len(backend.calls) == 1
    await mw.route(object(), _state(mid="u2"))
    assert len(backend.calls) == 2


async def test_router_empty_candidates_is_inert(isolated_home, monkeypatch):
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "enforce")
    backend = FakeBackend(answers=[_choice("model-b")])
    mw = ModelRouterMiddleware(backend, "s1")
    sentinel = object()
    assert await mw.route(sentinel, _state()) is sentinel
    assert backend.calls == []


# --------------------------------------------------------------------------- #
# ToolSelectionMiddleware
# --------------------------------------------------------------------------- #
def _tools(*names):
    return [SimpleNamespace(name=n) for n in names]


def _tool_cfg(isolated_home, monkeypatch, mode, **overrides):
    block = {"min_confidence": 0.6, "skip_below": 0, "max_selected": 24}
    block.update(overrides)
    _set_settings({"classifier": {"tools": block}}, isolated_home)
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", mode)


def _scores(scores: dict, qid: str = "tool_select"):
    return ScoreAnswer(question_id=qid, scores=scores)


async def test_tools_off_passthrough(isolated_home, monkeypatch):
    backend = FakeBackend()
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash")
    assert await mw.select(_state(), tools) is tools
    assert backend.calls == []


async def test_tools_skip_below_avoids_classification(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "enforce", skip_below=5)
    backend = FakeBackend(answers=[_scores({"read_file": 1.0})])
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash")
    assert await mw.select(_state(), tools) is tools
    assert backend.calls == []


async def test_tools_shadow_keeps_full_list(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "shadow")
    backend = FakeBackend(answers=[_scores({"read_file": 1.0})])
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash")
    assert await mw.select(_state(), tools) is tools
    log = paths.home() / "classifier_events.jsonl"
    event = json.loads(log.read_text().splitlines()[-1])
    assert event["kind"] == "tools" and event["mode"] == "shadow"
    assert event["decision"] == "2/2"


async def test_tools_enforce_removes_low_scores(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "enforce")
    backend = FakeBackend(
        answers=[_scores({"read_file": 0.9, "bash": 0.1, "write_file": 0.2})]
    )
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash", "write_file", "use_skill")
    kept = await mw.select(_state(), tools)
    assert [t.name for t in kept] == ["read_file", "use_skill"]  # use_skill: always-allow


async def test_tools_enforce_caps_max_selected(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "enforce", max_selected=2)
    backend = FakeBackend(
        answers=[_scores({n: 0.9 for n in ("a", "b", "c", "d")})]
    )
    mw = ToolSelectionMiddleware(backend, "s1")
    kept = await mw.select(_state(), _tools("d", "c", "b", "a"))
    assert [t.name for t in kept] == ["d", "c"]


async def test_tools_all_low_scores_degrades_to_full(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "enforce")
    backend = FakeBackend(answers=[_scores({"read_file": 0.1})])
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash")
    assert await mw.select(_state(), tools) is tools
    log = paths.home() / "classifier_events.jsonl"
    event = json.loads(log.read_text().splitlines()[-1])
    assert event["reason"] == "all_scores_below_threshold"


async def test_tools_backend_failure_returns_full(isolated_home, monkeypatch):
    _tool_cfg(isolated_home, monkeypatch, "enforce")
    backend = FakeBackend(exc="down")
    mw = ToolSelectionMiddleware(backend, "s1")
    tools = _tools("read_file", "bash")
    assert await mw.select(_state(), tools) is tools


def test_always_allow_tools_matches_tool_allowed_set():
    # 上面测试假定 use_skill/ask/render/workflow/artifact 永放行 —— 对齐
    # graph.tool_allowed 的常量，防止两处漂移。
    assert "use_skill" in ALWAYS_ALLOW_TOOLS
    assert "ask_user" in ALWAYS_ALLOW_TOOLS
    assert "render_widget" in ALWAYS_ALLOW_TOOLS
    assert "workflow_run" in ALWAYS_ALLOW_TOOLS
    assert "artifact_register" in ALWAYS_ALLOW_TOOLS
