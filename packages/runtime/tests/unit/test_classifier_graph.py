"""Real-graph integration tests for the classifier middlewares.

Drives the compiled graph (agent → permission → tools) with ScriptedChatModel,
asserting that routing/tool-selection change what the MODEL sees while the
degradation paths leave graph behaviour byte-identical to OFF."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage

from ginno_runtime import paths
from ginno_runtime.classifier.base import ClassifierModelBase, ClassifierUnavailable
from ginno_runtime.classifier.middlewares import (
    ModelRouterMiddleware,
    ToolSelectionMiddleware,
)
from ginno_runtime.classifier.questions import (
    ChoiceAnswer,
    ClassifierReply,
    ScoreAnswer,
)
from ginno_runtime.graph import build_graph
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.tools.builtin import build_builtin_tools
from ginno_runtime.tools.render_tools import attach_ref, render_widget

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class FakeBackend(ClassifierModelBase):
    def __init__(self, answers=None, exc=None):
        self.answers = list(answers or [])
        self.exc = exc

    async def classify(self, state, questions):
        if self.exc is not None:
            raise ClassifierUnavailable(self.exc)
        return list(self.answers)


class CapturingModel(ScriptedChatModel):
    """Records the message lists the model actually receives."""

    captured: list = []
    last_bound: list | None = None

    def bind_tools(self, tools, **kwargs):
        CapturingModel.last_bound = tools
        return super().bind_tools(tools, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        CapturingModel.captured.append(list(messages))
        return await super()._agenerate(messages, stop, run_manager, **kwargs)

    @staticmethod
    def bound_names():
        return [t.name for t in (CapturingModel.last_bound or [])]


def _set_settings(block: dict, isolated_home) -> None:
    p = paths.settings_path()
    raw = json.loads(p.read_text()) if p.exists() else {}
    raw.update(block)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw))


def _all_tools():
    return build_builtin_tools() + [render_widget, attach_ref]


def _cfg():
    return {"configurable": {"thread_id": "t-clf", "project_slug": "default", "agent_id": "dev"}}


def _input(mid="u1"):
    return {
        "messages": [HumanMessage(id=mid, content="帮我读一下 README 然后总结")],
        "workspace": "/tmp",
        "project_slug": "default",
        "agent_id": "dev",
        "pending_tool_calls": [],
    }


@pytest.fixture(autouse=True)
def _reset_captures(monkeypatch):
    CapturingModel.captured = []
    CapturingModel.last_bound = None
    monkeypatch.setattr(
        "ginno_runtime.classifier.middlewares._MODEL_CACHE", {}
    )


async def test_enforce_router_sends_turn_to_chosen_model(isolated_home, monkeypatch):
    _set_settings(
        {"classifier": {"router": {"candidates": ["model-a", "model-b"],
                                    "min_confidence": 0.7}}},
        isolated_home,
    )
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "enforce")
    model_b = ScriptedChatModel(scripts=[script(text="from-B")])
    monkeypatch.setattr(
        "ginno_runtime.models.build_model_by_name", lambda name="": model_b
    )
    model_a = CapturingModel(scripts=[script(text="from-A")])
    router = ModelRouterMiddleware(
        FakeBackend(answers=[ChoiceAnswer(question_id="model_route",
                                          option="model-b", confidence=0.9)]),
        "s1",
    )
    graph = build_graph(
        model=model_a, project_slug="default", workspace="/tmp", mcp_tools=[],
        all_tools=_all_tools(), router=router,
    )
    result = await graph.ainvoke(_input(), config=_cfg())
    assert result["messages"][-1].content == "from-B"
    assert CapturingModel.captured == []  # the turn never reached model A


async def test_shadow_router_behaves_like_off_but_records(isolated_home, monkeypatch):
    _set_settings(
        {"classifier": {"router": {"candidates": ["model-a", "model-b"]}}},
        isolated_home,
    )
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "shadow")
    model_a = ScriptedChatModel(scripts=[script(text="from-A")])
    router = ModelRouterMiddleware(
        FakeBackend(answers=[ChoiceAnswer(question_id="model_route",
                                          option="model-b", confidence=0.9)]),
        "s1",
    )
    graph = build_graph(
        model=model_a, project_slug="default", workspace="/tmp", mcp_tools=[],
        all_tools=_all_tools(), router=router,
    )
    result = await graph.ainvoke(_input(), config=_cfg())
    assert result["messages"][-1].content == "from-A"  # advice not applied
    log = paths.home() / "classifier_events.jsonl"
    events = [json.loads(l) for l in log.read_text().splitlines()]
    assert any(
        e["kind"] == "router" and e["decision"] == "model-b" and not e["applied"]
        for e in events
    )


async def test_enforce_tool_selection_annotates_tail_and_keeps_stable_layer(
    isolated_home, monkeypatch
):
    _set_settings({"classifier": {"tools": {"skip_below": 0}}}, isolated_home)
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "enforce")
    scores = {t.name: (0.1 if t.name == "write_file" else 0.9) for t in _all_tools()}
    selector = ToolSelectionMiddleware(
        FakeBackend(answers=[ScoreAnswer(question_id="tool_select", scores=scores)]),
        "s1",
    )
    model = CapturingModel(scripts=[script(text="done")])
    graph = build_graph(
        model=model, project_slug="default", workspace="/tmp", mcp_tools=[],
        all_tools=_all_tools(), tool_selector=selector,
    )
    await graph.ainvoke(_input(), config=_cfg())
    msgs = CapturingModel.captured[0]
    sys_text = msgs[0].content if isinstance(msgs[0].content, str) else str(msgs[0].content)
    tail = msgs[-1].content if isinstance(msgs[-1].content, str) else str(msgs[-1].content)
    # per-turn annotation on the tail HumanMessage …
    assert "write_file" not in CapturingModel.bound_names()
    assert "[Bound tools this turn:" in tail
    assert "read_file" in tail
    # … while the stable system layer keeps its superset declaration untouched
    assert "write_file" in sys_text


async def test_backend_failure_leaves_graph_identical_to_off(isolated_home, monkeypatch):
    monkeypatch.setenv("GINNO_CLASSIFIER_MODE", "enforce")
    selector = ToolSelectionMiddleware(FakeBackend(exc="provider down"), "s1")
    router = ModelRouterMiddleware(FakeBackend(exc="provider down"), "s1")

    async def _run(router_, selector_):
        model = CapturingModel(scripts=[script(text="plain answer")])
        CapturingModel.last_bound = None
        graph = build_graph(
            model=model, project_slug="default", workspace="/tmp", mcp_tools=[],
            all_tools=_all_tools(), router=router_, tool_selector=selector_,
        )
        result = await graph.ainvoke(_input(), config=_cfg())
        return result["messages"][-1].content, CapturingModel.bound_names()

    degraded_text, degraded_bound = await _run(router, selector)
    off_text, off_bound = await _run(None, None)
    assert degraded_text == off_text
    assert degraded_bound == off_bound
