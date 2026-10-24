# -*- coding: utf-8 -*-
"""Manual smoke test: classifier layer over Jev's native Decisions API.

用法：
    uv run --extra docs python scripts/jev_openrouter_smoke.py <OPENROUTER_KEY>

搭建一次性 GINNO_HOME（临时目录，不碰真实 ~/.ginno），classifier 后端为
``jev``（直连 OpenRouter ``POST /api/alpha/decisions``，state+questions
原生形态，不经 openai-compatible chat completions）。依次验证：
  1. 直接调 JevDecisionsModel.classify（Choice + Score 一次问两题）
  2. build_middlewares 的 SHADOW 路径（router + tool selector 各一次）
打印延迟、答案与 classifier_events.jsonl。
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from ginno_runtime import jsonx as json

KEY = os.environ.get("OPENROUTER_API_KEY") or (sys.argv[1] if len(sys.argv) > 1 else "")
if not KEY:
    raise SystemExit("usage: python scripts/jev_openrouter_smoke.py <OPENROUTER_KEY>")

MODEL = "~typesafe/jev-latest"
CANDIDATES = ["qwen-flash", "claude-sonnet"]
TOOLS = ["read_file", "bash", "write_file", "web_search", "render_widget"]

home = Path(tempfile.mkdtemp(prefix="ginno-jev-smoke-"))
(home / "settings.json").write_text(
    json.dumps(
        {
            "classifier": {
                "backend": "jev",
                "model": MODEL,
                "api_key": KEY,
                "timeout_s": 20,
                "router": {"enabled": True, "candidates": CANDIDATES, "min_confidence": 0.7},
                "tools": {"enabled": True, "min_confidence": 0.6, "skip_below": 0,
                          "max_selected": 24},
                "shadow_log": "classifier_events.jsonl",
            }
        }
    ),
    encoding="utf-8",
)
os.environ["GINNO_HOME"] = str(home)
os.environ["GINNO_CLASSIFIER_MODE"] = "shadow"
os.environ.pop("GINNO_FAKE_LLM", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langchain_core.messages import HumanMessage  # noqa: E402

from ginno_runtime.classifier.jev import JevDecisionsModel  # noqa: E402
from ginno_runtime.classifier.middlewares import build_middlewares  # noqa: E402
from ginno_runtime.classifier.questions import (  # noqa: E402
    ChoiceQuestion,
    ScoreQuestion,
)


async def main() -> None:
    state = {
        "messages": [
            HumanMessage(
                id="m1",
                content="帮我把 ~/Downloads 里的发票 PDF 按月份重命名，再汇总成表格",
            )
        ]
    }
    questions = [
        ChoiceQuestion(
            id="model_route",
            prompt="Which model is best suited to answer this request?",
            options=CANDIDATES,
        ),
        ScoreQuestion(
            id="tool_select",
            prompt="Score how likely each tool is needed to handle this input "
            "(0 = unrelated, 1 = clearly required).",
            items=TOOLS,
        ),
    ]

    print(f"GINNO_HOME = {home}")
    print(f"backend    = jev (decisions api)  model = {MODEL}\n")

    backend = JevDecisionsModel(api_key=KEY, model=MODEL, timeout_s=20)
    t0 = time.perf_counter()
    answers = await backend.classify(state, questions)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[direct classify] {dt:.0f}ms")
    for a in answers:
        print("  ", a.model_dump())

    router, selector = build_middlewares("smoke-session")
    sentinel = object()
    t0 = time.perf_counter()
    routed = await router.route(sentinel, state, None)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[router.route]   {dt:.0f}ms  applied={routed is not sentinel} (shadow: advice only)")

    tools = [type("T", (), {"name": n})() for n in TOOLS]
    t0 = time.perf_counter()
    kept = await selector.select(state, tools)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[tools.select]   {dt:.0f}ms  kept={[t.name for t in kept]} (shadow: advice only)")

    log = home / "classifier_events.jsonl"
    print("\n--- classifier_events.jsonl ---")
    for line in log.read_text().splitlines():
        print(" ", line)


asyncio.run(main())
