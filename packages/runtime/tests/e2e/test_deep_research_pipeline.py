"""E2E: the "deep-research-pipeline" complex workflow scenario
(docs/design/world-state-plan.md follow-up; scenario design: 主题深挖 →
AI 评审返工 → 人工兜底 → 知识库发布).

Exercises the FULL engine surface against the real compiled graph:

* step (agent) nodes with WRITE_JSON context writes
* loop over context list with per-item goal rendering
* llm nodes (synthesize / digest)
* branch with ordered cases + transform carrying the retry counter
* a bounded rework cycle (revise → judge back-edge, cut off by tries >= 2)
* human node → interrupt → paused → resume with context_patch
* supervisor coerce on a schema-required input
* API creation of the DSL (version 1)

Model calls are scripted (ScriptedChatModel), so script order = node order:
prep → probe×N → synthesize → judge → [revise → judge]… → publish → digest.
"""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import engine

pytestmark = pytest.mark.e2e


def pipeline_dsl() -> dict:
    """The pure DSL (no test-only node types) — also what the API receives."""
    return {
        "dsl_version": "1",
        "name": "deep-research-pipeline",
        "description": "主题深挖 → AI 评审返工 → 人工兜底 → 知识库发布",
        "entry": "prep",
        "context": {
            "schema": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "scope": {"type": "string", "default": "近 30 天,中英文来源"},
                    "questions": {"type": "array", "items": {"type": "string"}},
                    "findings": {"type": "array", "items": {"type": "object"}},
                    "draft": {"type": "string"},
                    "score": {"type": "number"},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "tries": {"type": "number", "default": 0},
                    "human_decision": {"type": "string"},
                    "page_path": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["topic"],
            },
            "initial": {"topic": "本地 AI Agent 的上下文管理", "tries": 0},
        },
        "nodes": [
            {
                "id": "prep",
                "type": "step",
                "agent": "research",
                "goal": (
                    "把研究主题「{{context.topic}}」拆成 3-5 个互不重叠、可独立检索的"
                    "子问题;结合 {{context.scope}} 界定范围。"
                ),
                "writes": ["questions", "scope"],
            },
            {
                "id": "research",
                "type": "loop",
                "over": "context.questions",
                "as": "q",
                "body": "probe",
                "max_iters": 8,
            },
            {
                "id": "probe",
                "type": "step",
                "agent": "research",
                "goal": (
                    "针对子问题「{{q}}」做 web 检索与阅读,产出一条 finding"
                    "({question, sources, note}),并把包含全部已有发现的完整列表写回。"
                ),
                "writes": ["findings"],
            },
            {
                "id": "synthesize",
                "type": "llm",
                "prompt": (
                    "基于以下调研发现写一份结构化报告草稿(结论先行、分主题、附来源)。"
                    "主题:{{context.topic}}\n发现:{{context.findings}}"
                ),
                "output": "draft",
            },
            {
                "id": "judge",
                "type": "step",
                "agent": "research",
                "goal": (
                    "评审 context.draft:准确性/覆盖度/结构,打分 0-10 并列出不超过 3 条"
                    "最要命的 issues。只输出评分与问题,不改稿。"
                ),
                "writes": ["score", "issues"],
            },
            {
                "id": "gate",
                "type": "branch",
                "cases": [
                    {"when": "context.score >= 8", "then": "publish"},
                    {"when": "context.tries >= 2", "then": "review"},
                    {
                        "when": "context.score < 8",
                        "then": "revise",
                        "transform": {"expr": {"tries": "context.tries + 1"}},
                    },
                ],
                "default": "review",
            },
            {
                "id": "revise",
                "type": "step",
                "agent": "writer",
                # tries arrives via the gate's transform (eff); the step persists
                # it back into context through WRITE_JSON — transform carries,
                # step persists.
                "goal": (
                    "针对 issues({{context.issues}})修改草稿,产出新版 draft;"
                    "WRITE_JSON 必须同时写回 \"tries\": {{tries}}。"
                ),
                "writes": ["draft", "tries"],
            },
            {
                "id": "review",
                "type": "human",
                "question": (
                    "两轮自动修改后仍未达 8 分。直接发布、给出修改方向"
                    "(context_patch.human_decision='revise'+意见)、还是终止?"
                ),
            },
            {
                "id": "route",
                "type": "branch",
                "cases": [
                    {"when": "context.human_decision == 'publish'", "then": "publish"}
                ],
                "default": "revise",
            },
            {
                "id": "publish",
                "type": "step",
                "agent": "writer",
                "goal": (
                    "把最终稿写入知识库 Ginno/Wiki/research/ 下(文件名用主题 slug),"
                    "写回 page_path。"
                ),
                "writes": ["page_path"],
            },
            {
                "id": "digest",
                "type": "llm",
                "prompt": (
                    "用一句话总结这次研究的结论,并说明报告已发布到 {{context.page_path}}。"
                    "主题:{{context.topic}}"
                ),
                "output": "summary",
            },
            {"id": "done", "type": "pass"},
        ],
        "edges": [
            {"from": "prep", "to": "research"},
            {"from": "research", "to": "synthesize"},
            {"from": "synthesize", "to": "judge"},
            {"from": "judge", "to": "gate"},
            {"from": "revise", "to": "judge"},
            {"from": "review", "to": "route"},
            {"from": "publish", "to": "digest"},
            {"from": "digest", "to": "done"},
        ],
        "supervisor": {"enabled": True, "mode": "auto"},
    }


def _wjson(payload: dict) -> str:
    import json

    return f"\nWRITE_JSON {json.dumps(payload, ensure_ascii=False)}"


# --------------------------------------------------------------------------- #
# 0. DSL validates; API creates it as version 1
# --------------------------------------------------------------------------- #
def test_dsl_validates_and_creates_via_api(client):
    d = wf_dsl.normalize_dsl(pipeline_dsl())
    assert wf_dsl.validate_dsl(d) == []

    r = client.post(
        "/api/workflows", json={"name": "deep-research-pipeline", "dsl": pipeline_dsl()}
    )
    assert r.status_code == 200, r.text
    wf = r.json()["workflow"]
    assert wf["version"] == 1
    wf_id = wf["id"]

    versions = client.get(f"/api/workflows/{wf_id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1]
    got = client.get(f"/api/workflows/{wf_id}").json()["workflow"]
    node_ids = {n["id"] for n in got["dsl"]["nodes"]}
    assert {"prep", "research", "probe", "gate", "review", "publish"} <= node_ids


# --------------------------------------------------------------------------- #
# 1. Happy path with ONE rework cycle: judge fails once, revise, judge passes
# --------------------------------------------------------------------------- #
async def test_happy_path_with_one_rework_cycle(isolated_home):
    model = ScriptedChatModel(
        scripts=[
            # prep
            script(text="拆解完成" + _wjson({
                "questions": ["主流上下文压缩做法?", "prefix cache 的工程约束?"],
                "scope": "近 30 天",
            })),
            # probe #1 (writes the full accumulated findings list each time)
            script(text=_wjson({"findings": [
                {"question": "主流上下文压缩做法?", "sources": ["u1"], "note": "摘要1"},
            ]})),
            # probe #2
            script(text=_wjson({"findings": [
                {"question": "主流上下文压缩做法?", "sources": ["u1"], "note": "摘要1"},
                {"question": "prefix cache 的工程约束?", "sources": ["u2"], "note": "摘要2"},
            ]})),
            # synthesize (llm)
            script(text="报告草稿 v1:结论先行……"),
            # judge #1 — fails at 5
            script(text=_wjson({"score": 5, "issues": ["缺少来源交叉验证", "结论过于笼统"]})),
            # revise #1 — gate transform passes tries=1; step persists it
            script(text="修改完成" + _wjson({"draft": "报告草稿 v2", "tries": 1})),
            # judge #2 — passes at 9
            script(text=_wjson({"score": 9, "issues": []})),
            # publish
            script(text=_wjson({"page_path": "Ginno/Wiki/research/agent-context.md"})),
            # digest (llm)
            script(text="结论:上下文压缩以 prefix cache 友好为首要约束;报告已发布。"),
        ]
    )
    events = []
    async for ev in engine.run_workflow(
        pipeline_dsl(), run_id="drp-happy", model=model, tools=[], project_slug="e2e-drp"
    ):
        events.append(ev)

    kinds = [e["kind"] for e in events]
    enters = [e["node_id"] for e in events if e["kind"] == "node_enter"]

    # two loop iterations over the two questions
    assert sum(1 for e in events if e["kind"] == "loop_iter") == 2
    # order: prep → loop/probe×2 → synthesize → judge → revise → judge → publish → digest
    assert enters[0] == "prep"
    assert enters.count("probe") == 2
    assert enters.count("judge") == 2
    assert "revise" in enters and enters.count("revise") == 1
    # the rework back-edge: revise sits between the two judges
    last_judge = len(enters) - 1 - enters[::-1].index("judge")
    assert enters.index("revise") > enters.index("judge")
    assert enters.index("publish") > last_judge
    # happy path never reaches the human node
    assert "review" not in enters
    assert "interrupt" not in kinds
    # context writes across the whole pipeline
    written = {k for e in events if e["kind"] == "context_write" for k in e["keys"]}
    expected_writes = {
        "questions", "scope", "findings", "draft", "score", "issues",
        "tries", "page_path", "summary",
    }
    assert expected_writes <= written
    assert kinds[-1] == "done"


# --------------------------------------------------------------------------- #
# 2. Human fallback: two failed reworks → human interrupt → resume(publish)
# --------------------------------------------------------------------------- #
async def test_human_fallback_then_publish(isolated_home):
    model = ScriptedChatModel(
        scripts=[
            script(text="拆解" + _wjson({"questions": ["q-a?", "q-b?"]})),
            script(text=_wjson({"findings": [{"question": "q-a?", "note": "n1"}]})),
            script(text=_wjson({"findings": [
                {"question": "q-a?", "note": "n1"}, {"question": "q-b?", "note": "n2"},
            ]})),
            script(text="草稿 v1"),
            script(text=_wjson({"score": 4, "issues": ["覆盖不足"]})),       # judge #1
            script(text=_wjson({"draft": "草稿 v2", "tries": 1})),           # revise #1
            script(text=_wjson({"score": 5, "issues": ["仍缺来源"]})),        # judge #2
            script(text=_wjson({"draft": "草稿 v3", "tries": 2})),           # revise #2
            # judge #3 → tries>=2 → human
            script(text=_wjson({"score": 6, "issues": ["结构松散"]})),
            # — paused at review; after resume(publish): —
            script(text=_wjson({"page_path": "Ginno/Wiki/research/q.md"})),  # publish
            script(text="一句话收尾"),                                        # digest
        ]
    )
    dsl = pipeline_dsl()
    events = []
    async for ev in engine.run_workflow(
        dsl, run_id="drp-human", model=model, tools=[], project_slug="e2e-drp"
    ):
        events.append(ev)

    kinds = [e["kind"] for e in events]
    enters = [e["node_id"] for e in events if e["kind"] == "node_enter"]
    # bounded rework: exactly two revise passes before the human gate
    assert enters.count("revise") == 2
    assert enters.count("judge") == 3
    # the human node announces itself via an interrupt event, not node_enter
    assert "interrupt" in kinds
    intr = next(e for e in events if e["kind"] == "interrupt")
    assert "8 分" in intr["question"]
    assert kinds[-1] == "paused"  # NOT done — the run is suspended, resumable

    # user decision: publish as-is
    resumed = []
    async for ev in engine.resume_workflow(
        dsl,
        run_id="drp-human",
        model=model,
        tools=[],
        resume_value={"decision": "continue", "context_patch": {"human_decision": "publish"}},
        project_slug="e2e-drp",
    ):
        resumed.append(ev)

    r_enters = [e["node_id"] for e in resumed if e["kind"] == "node_enter"]
    assert "resume" in [e["kind"] for e in resumed]
    # route branch took the publish case; no third revise
    assert "publish" in r_enters and "digest" in r_enters
    assert "revise" not in r_enters
    written = {k for e in resumed if e["kind"] == "context_write" for k in e["keys"]}
    assert {"page_path", "summary"} <= written
    assert resumed[-1]["kind"] == "done"


# --------------------------------------------------------------------------- #
# 3. Human sends it back once more (bounded by the human itself), then passes
# --------------------------------------------------------------------------- #
async def test_human_sends_back_then_passes(isolated_home):
    model = ScriptedChatModel(
        scripts=[
            script(text="拆解" + _wjson({"questions": ["q-a?"]})),
            script(text=_wjson({"findings": [{"question": "q-a?", "note": "n1"}]})),
            script(text="草稿 v1"),
            script(text=_wjson({"score": 4, "issues": ["i1"]})),
            script(text=_wjson({"draft": "草稿 v2", "tries": 1})),
            script(text=_wjson({"score": 5, "issues": ["i2"]})),
            script(text=_wjson({"draft": "草稿 v3", "tries": 2})),
            script(text=_wjson({"score": 6, "issues": ["i3"]})),   # → human
            # — resume(revise + 意见) → revise #3 → judge passes —
            script(text=_wjson({"draft": "草稿 v4", "tries": 3})),
            script(text=_wjson({"score": 9, "issues": []})),
            script(text=_wjson({"page_path": "Ginno/Wiki/research/q.md"})),
            script(text="收尾"),
        ]
    )
    dsl = pipeline_dsl()
    events = []
    async for ev in engine.run_workflow(
        dsl, run_id="drp-human2", model=model, tools=[], project_slug="e2e-drp"
    ):
        events.append(ev)
    assert events[-1]["kind"] == "paused"

    resumed = []
    async for ev in engine.resume_workflow(
        dsl,
        run_id="drp-human2",
        model=model,
        tools=[],
        resume_value={
            "decision": "continue",
            "context_patch": {"human_decision": "revise", "issues": ["补充国内案例"]},
        },
        project_slug="e2e-drp",
    ):
        resumed.append(ev)

    r_enters = [e["node_id"] for e in resumed if e["kind"] == "node_enter"]
    # route default → revise; then judge scores 9 → gate case① → publish
    assert r_enters[:2] == ["revise", "judge"]
    assert "publish" in r_enters and "review" not in r_enters
    assert resumed[-1]["kind"] == "done"


# --------------------------------------------------------------------------- #
# 4. Supervisor coerces a missing required input (extension-node pattern)
# --------------------------------------------------------------------------- #
async def test_supervisor_coerces_missing_topic(isolated_home):
    from ginno_runtime.workflows.nodes import BaseNode, register_node

    @register_node
    class TopicIntake(BaseNode):
        type = "drp_topic_intake"
        inputs_schema = {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string", "default": "本周 AI Agent 生态进展"}},
        }

        @staticmethod
        async def execute(node, cctx, state, config, eff):
            ctx = dict(state.get("context") or {})
            return {
                "context": {**ctx, "topic": eff.get("topic")},
                "events": [],
                "__output__": eff,
            }

    dsl = {
        "name": "drp-supervisor-demo",
        "entry": "intake",
        "context": {
            "schema": {"type": "object", "properties": {"topic": {"type": "string"}}},
            "initial": {},
        },
        "nodes": [
            {"id": "intake", "type": "drp_topic_intake"},
            {
                "id": "digest",
                "type": "llm",
                "prompt": "一句话研究计划:{{context.topic}}",
                "output": "summary",
            },
            {"id": "done", "type": "pass"},
        ],
        "edges": [{"from": "intake", "to": "digest"}, {"from": "digest", "to": "done"}],
    }
    model = ScriptedChatModel(scripts=[script(text="先调研再成文。")])
    events = []
    async for ev in engine.run_workflow(
        dsl, run_id="drp-super", model=model, tools=[], project_slug="e2e-drp"
    ):
        events.append(ev)

    coerced = [e for e in events if e["kind"] == "supervisor_intervene"]
    assert coerced and coerced[0]["action"] == "coerce"
    written = {k for e in events if e["kind"] == "context_write" for k in e["keys"]}
    assert "summary" in written
    assert events[-1]["kind"] == "done"
