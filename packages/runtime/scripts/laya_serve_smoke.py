# -*- coding: utf-8 -*-
"""Manual smoke test: classifier layer over a laya HTTP service.

用法（token 从环境或参数传入，不落盘；BASE_URL 是完整 endpoint URL）：
    # 本地开源 laya-serve（默认，无鉴权）
    uv run --extra docs python scripts/laya_serve_smoke.py
    # 网关型部署（Bearer 必须）——地址按部署文档配置，不写死在代码里
    LAYA_API_KEY=<token> uv run --extra docs python scripts/laya_serve_smoke.py \
        http://<gateway-host>/<path>/api/v0.1/predictions

搭建一次性 GINNO_HOME（临时目录，不碰真实 ~/.ginno），classifier 后端为
``laya``（POST 到 base_url；服务端按输入语言自动路由 checkpoint——中文走
multilingual）。依次验证：
  1. 直接调 LayaSystemOneModel.classify（Choice + Score 一次问两题），
     英文与中文各一轮——顺带观察自动路由（routing.model）是否正确。
  2. build_middlewares 的 SHADOW 路径（router + tool selector 各一次）
打印延迟、答案（含 answer_confidence 与熵 confidence 的对照）与
classifier_events.jsonl。
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

from ginno_runtime import jsonx as json

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else "") or ""  # "" → SF 网关默认
API_KEY = os.environ.get("LAYA_API_KEY") or (sys.argv[2] if len(sys.argv) > 2 else "")
if not API_KEY:
    raise SystemExit(
        "usage: LAYA_API_KEY=<token> python scripts/laya_serve_smoke.py "
        "[BASE_URL] [TOKEN]"
    )

CANDIDATES = ["qwen-flash", "claude-sonnet"]
TOOLS = ["read_file", "bash", "write_file", "web_search", "render_widget"]

home = Path(tempfile.mkdtemp(prefix="ginno-laya-smoke-"))
(home / "settings.json").write_text(
    json.dumps(
        {
            "classifier": {
                "backend": "laya",
                "api_key": API_KEY,
                "base_url": BASE_URL,
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

from ginno_runtime.classifier.base import ClassifierUnavailable  # noqa: E402
from ginno_runtime.classifier.laya import LayaSystemOneModel  # noqa: E402
from ginno_runtime.classifier.middlewares import build_middlewares  # noqa: E402
from ginno_runtime.classifier.questions import (  # noqa: E402
    ChoiceQuestion,
    ScoreQuestion,
)

QUESTIONS = [
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

STATES = {
    "en": "Rename the invoice PDFs in ~/Downloads by month, then aggregate "
          "them into a spreadsheet",
    "zh": "帮我把 ~/Downloads 里的发票 PDF 按月份重命名，再汇总成表格",
}


async def classify_round(backend: LayaSystemOneModel, label: str, text: str) -> None:
    state = {"messages": [HumanMessage(id=f"m-{label}", content=text)]}
    t0 = time.perf_counter()
    try:
        answers = await backend.classify(state, QUESTIONS)
    except ClassifierUnavailable as e:
        print(f"[{label}] FAILED: {e}")
        return
    dt = (time.perf_counter() - t0) * 1000
    print(f"[{label}] {dt:.0f}ms")
    for a in answers:
        print("  ", a.model_dump())


async def main() -> None:
    print(f"GINNO_HOME = {home}")
    print(f"backend    = laya  endpoint = {BASE_URL or '(default: local laya-serve)'}\n")

    backend = LayaSystemOneModel(api_key=API_KEY, base_url=BASE_URL, timeout_s=20)
    # 英文轮 + 中文轮：验证服务端语言自动路由（中文应走 multilingual）
    await classify_round(backend, "direct/en", STATES["en"])
    await classify_round(backend, "direct/zh", STATES["zh"])

    router, selector = build_middlewares("smoke-session")
    state = {"messages": [HumanMessage(id="m1", content=STATES["zh"])]}
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


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
