#!/usr/bin/env python3
"""Code Mode 确定性收益实测（选项 B）——全程 fake-LLM，零真实 LLM 调用。

同一 5 步工具链场景「搜索→读文件→改→跑测试→复核」，对比两模式：
  traditional：逐 tool_call，每步全量结果回灌上下文（N 次 LLM 往返）
  code-mode  ：LLM 写一次代码，执行器内跑全部调用，仅 return 值进上下文

三项指标：上下文占用（tiktoken cl100k_base）、LLM 往返次数、端到端延迟。
延迟口径诚实声明：fake-LLM 无真实推理等待，两模式墙钟≈工具执行耗时；
code-mode 额外含 RPC/子进程开销。真实延迟收益 ∝ 往返次数 × 单次 LLM 延迟，
本脚本不虚构该常数——往返次数才是延迟/成本的决定性代理指标。

复跑：uv run --directory packages/runtime python scripts/code_mode_bench.py
（明早配额重置后如需真实 LLM 验证，把 fake_llm_* 换成真实调用即可，骨架不变。）
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import tiktoken

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ginno_runtime.code_mode import run_repl  # noqa: E402

ENC = tiktoken.get_encoding("cl100k_base")
SYSTEM = "You are a coding agent. Use tools to edit code and verify with tests."
TASK = "把 retryPolicy 的 maxAttempts 从 3 改成 5，跑测试确认，汇总改了哪些文件。"


def tokens(messages: list[dict]) -> int:
    """chat 消息列表的 token 近似（role+content 拼接，cl100k_base）。"""
    text = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    return len(ENC.encode(text))


# ── 确定性 fixture + 工具集（真实文件操作，输出量级贴近真实场景）──────────
def make_fixture(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    # 主文件 + 12 个干扰文件，让 search 返回大量行（真实搜索常态）
    (src / "retry.py").write_text(
        "def retryPolicy():\n    config = {\n        'maxAttempts': 3,\n"
        "        'backoff': 2,\n    }\n    return config\n"
    )
    for i in range(12):
        (src / f"mod_{i}.py").write_text(
            f"# module {i}\nfrom retry import retryPolicy\n"
            f"def handler_{i}():\n    return retryPolicy()['maxAttempts'] * {i}\n"
        )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_retry.py").write_text(
        "from src.retry import retryPolicy\n"
        "def test_attempts():\n    assert retryPolicy()['maxAttempts'] == 5\n"
    )


def build_tools(root: Path) -> dict:
    def search(a):
        pat, path = a["pattern"], a.get("path", "src")
        out = []
        for f in sorted((root / path).rglob("*.py")):
            for n, ln in enumerate(f.read_text().splitlines(), 1):
                if pat in ln:
                    out.append(f"{f.relative_to(root)}:{n}:{ln}")
        return "\n".join(out)  # 全量回灌（traditional 的上下文污染源）

    def read_file(a):
        return (root / a["path"]).read_text()

    def edit_file(a):
        p = root / a["path"]
        txt = p.read_text()
        if a["old"] not in txt:
            return {"ok": False, "error": "old not found"}
        p.write_text(txt.replace(a["old"], a["new"]))
        return {"ok": True, "path": a["path"], "diff": f"- {a['old']}\n+ {a['new']}"}

    def run_test(a):
        # 确定性「测试通过」+ 冗长输出（真实 pytest 输出量级）
        lines = [f"tests/test_retry.py::test_case_{i} PASSED" for i in range(40)]
        lines += ["", "==== 40 passed in 1.23s ===="]
        return "\n".join(lines)

    return {"search": search, "read_file": read_file, "edit_file": edit_file, "run_test": run_test}


# 5 步工具链（两模式共用同一确定性序列）
STEPS = [
    ("search", {"pattern": "retryPolicy", "path": "src"}),
    ("read_file", {"path": "src/retry.py"}),
    ("edit_file", {"path": "src/retry.py", "old": "'maxAttempts': 3", "new": "'maxAttempts': 5"}),
    ("run_test", {"path": "tests/test_retry.py"}),
    ("search", {"pattern": "maxAttempts", "path": "src"}),
]

# code-mode：LLM 一次性写出的编排代码（与 STEPS 等价，return-only 小摘要）
CODE = """
hits = tool("search", {"pattern": "def retryPolicy", "path": "src"})
target = hits.splitlines()[0].split(":")[0]
tool("read_file", {"path": target})
ed = tool("edit_file", {"path": target, "old": "'maxAttempts': 3", "new": "'maxAttempts': 5"})
tests = tool("run_test", {"path": "tests/test_retry.py"})
verify = tool("search", {"pattern": "maxAttempts", "path": "src"})
return {"edited": [target], "ok": ed["ok"], "tests_passed": "40 passed" in tests,
        "attempts_now": 5, "verify_hits": len(verify.splitlines())}
"""


def fake_llm_traditional(step_idx: int) -> dict:
    """fake-LLM：按步序确定性吐下一个 tool_call（零真实调用）。"""
    name, args = STEPS[step_idx]
    return {"role": "assistant", "content": "", "tool_call": {"name": name, "args": args}}


def run_traditional(tools: dict) -> dict:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    t0 = time.perf_counter()
    rounds = 0
    for i in range(len(STEPS)):
        call = fake_llm_traditional(i)
        rounds += 1  # 一次 LLM 往返生成 tool_call
        msgs.append({"role": "assistant", "content": json.dumps(call["tool_call"], ensure_ascii=False)})
        result = tools[call["tool_call"]["name"]](call["tool_call"]["args"])
        msgs.append({"role": "tool", "content": result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)})
    # 最终汇总往返
    rounds += 1
    msgs.append({"role": "assistant", "content": "已把 maxAttempts 改为 5，测试通过，改动 src/retry.py。"})
    wall = time.perf_counter() - t0
    return {"tokens": tokens(msgs), "rounds": rounds, "wall_s": wall, "msg_count": len(msgs)}


def run_code_mode(tools: dict) -> dict:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    rounds = 1  # LLM 写一次代码
    msgs.append({"role": "assistant", "content": CODE})
    res = run_repl(CODE, tools, timeout_s=30)
    rounds += 1  # 读 return 值后汇总
    msgs.append({"role": "tool", "content": res.to_tool_output()})
    msgs.append({"role": "assistant", "content": "已把 maxAttempts 改为 5，测试通过，改动 src/retry.py。"})
    return {"tokens": tokens(msgs), "rounds": rounds, "wall_s": res.wall_s,
            "inner_calls": res.inner_calls, "error": res.error, "return_value": res.value,
            "msg_count": len(msgs)}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cm-bench-") as td:
        root = Path(td)
        make_fixture(root)
        tools = build_tools(root)
        trad = run_traditional(tools)
        # code-mode 会改文件，重置 fixture 保证两模式独立同起点
        make_fixture(root)
        cm = run_code_mode(build_tools(root))

    tok_save = 1 - cm["tokens"] / trad["tokens"]
    round_save = 1 - cm["rounds"] / trad["rounds"]
    print("=" * 64)
    print("Code Mode 确定性收益实测（fake-LLM，零真实调用，cl100k_base）")
    print("=" * 64)
    print(f"{'指标':<22}{'traditional':>16}{'code-mode':>16}")
    print(f"{'上下文 tokens':<22}{trad['tokens']:>16}{cm['tokens']:>16}")
    print(f"{'LLM 往返次数':<22}{trad['rounds']:>16}{cm['rounds']:>16}")
    print(f"{'消息条数':<22}{trad['msg_count']:>16}{cm['msg_count']:>16}")
    print(f"{'内层工具调用(留执行器)':<22}{len(STEPS):>16}{cm['inner_calls']:>16}")
    print(f"{'端到端墙钟(s)*':<22}{trad['wall_s']:>16.4f}{cm['wall_s']:>16.4f}")
    print("-" * 64)
    print(f"上下文节省: {tok_save:.1%}   往返节省: {round_save:.1%}")
    print(f"code-mode return 值: {json.dumps(cm['return_value'], ensure_ascii=False)}")
    if cm["error"]:
        print(f"⚠️ code-mode 执行错误: {cm['error']}")
    print("* 墙钟为 fake-LLM 确定性执行耗时（无真实推理等待）；code-mode 含")
    print("  RPC/子进程开销。真实延迟收益 ∝ 往返节省 × 单次 LLM 延迟，不在此虚构。")
    print("=" * 64)
    # 决策阈值：上下文节省 ≥50% 且 往返节省 ≥60% → 推荐 A
    if tok_save >= 0.50 and round_save >= 0.60 and not cm["error"]:
        print("建议: A（进入 P0 施工）——上下文与往返收益均达阈值。")
    else:
        print("建议: C（归档）——收益未达阈值或执行有误，理由见上表。")


if __name__ == "__main__":
    main()
