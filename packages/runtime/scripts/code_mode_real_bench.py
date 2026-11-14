#!/usr/bin/env python3
"""Code Mode 真实 Ginno 对比基准（magpie qwen3.8-flash，真实 LLM）。

用真实 Ginno agent（ginno exec 隔离模式，源码含 repl 接入）跑同一任务两 case：
  A traditional：逐步独立工具调用（搜索→读→改→跑检查）
  B code-mode  ：用 repl 工具一段 Python 编排一次完成，return-only

从 ginno exec 的 stream-json 解析真实数据：
  - 往返次数 = assistant 事件数（每个 message.end 一条）
  - 工具调用数 = tool_use 块总数
  - 累计输入 tokens = 各 turn input_tokens 之和（真实成本驱动：每 turn 重读上下文）
  - 累计输出 tokens = 各 turn output_tokens 之和

env：GINNO_EXEC_BASE_URL/API_KEY/MODEL（magpie），GINNO_CODE_MODE=1（启 repl）。
复跑：uv run --directory packages/runtime python scripts/code_mode_real_bench.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
BENCH_ENV = Path("/Users/chenchuansong/workspace/workbuddy-bench/.env")
MODEL = "qwen-token-plan/qwen3.8-flash"
BASE_URL = "http://127.0.0.1:3425/v1"


def magpie_key() -> str:
    for ln in BENCH_ENV.read_text().splitlines():
        if ln.startswith("GLM_ZHIPU_API_KEY="):
            return ln.split("=", 1)[1].strip()
    raise SystemExit("magpie key not found in workbuddy-bench/.env")


def make_fixture(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "retry.py").write_text(
        "def retryPolicy():\n    config = {\n        'maxAttempts': 3,\n"
        "        'backoff': 2,\n    }\n    return config\n"
    )
    for i in range(int(os.environ.get('CM_NOISE', '12'))):
        (src / f"mod_{i}.py").write_text(
            f"# module {i}\nfrom retry import retryPolicy\n"
            f"def handler_{i}():\n    return retryPolicy()['maxAttempts'] * {i}\n"
        )
    (root / "check.py").write_text(
        "import sys\nsys.path.insert(0, 'src')\nfrom retry import retryPolicy\n"
        "n = retryPolicy()['maxAttempts']\n"
        "assert n == 5, f'maxAttempts={n}, expected 5'\n"
        "for i in range(int(__import__('os').environ.get('CM_CHECKLINES','30'))):\n    print(f'check_case_{i} PASSED ' + 'x'*40)\n"
        "print('==== ALL CHECKS PASSED ====')\n"
    )


PROMPT_A = (
    "把 src/retry.py 里 retryPolicy 的 maxAttempts 从 3 改成 5，然后运行 "
    "`python check.py` 确认通过。请逐步用独立的工具调用完成：先 grep_files 搜索定位、"
    "再 read_file 读文件、再 edit_file 编辑、再 bash 跑 check.py。每一步用单独的工具调用，"
    "不要用 repl。最后简短汇总。"
)
PROMPT_B = (
    "把 src/retry.py 里 retryPolicy 的 maxAttempts 从 3 改成 5，然后运行 "
    "`python check.py` 确认通过。请使用 repl 工具：写一段 Python 编排代码，一次性完成 "
    "grep_files 搜索→read_file→edit_file→bash 跑 check.py→复核，中间结果留在代码里，"
    "只 return 一个小摘要（改了哪个文件、是否通过）。不要用多次独立工具调用。"
)


def run_case(name: str, prompt: str, key: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"cm-real-{name}-") as td:
        root = Path(td)
        make_fixture(root)
        env = dict(
            os.environ,
            GINNO_EXEC_BASE_URL=BASE_URL,
            GINNO_EXEC_API_KEY=key,
            GINNO_EXEC_MODEL=MODEL,
            GINNO_CODE_MODE="1",
            GINNO_EXEC_NO_ATTACH="1",
        )
        env.pop("GINNO_FAKE_LLM", None)
        cmd = [
            sys.executable, "-m", "ginno_runtime", "exec",
            "--no-attach", "--no-world-state", "--cwd", str(root),
            "--settle-timeout", "120", "--", prompt,
        ]
        proc = subprocess.run(
            cmd, cwd=str(RUNTIME), env=env, capture_output=True, text=True, timeout=420
        )
        return parse_stream(proc.stdout, name, proc.returncode, proc.stderr)


def parse_stream(out: str, name: str, rc: int, stderr: str) -> dict:
    rounds = 0
    tool_calls = 0
    tool_names: list[str] = []
    in_sum = out_sum = 0
    final_text = ""
    repl_used = False
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            rounds += 1
            msg = ev.get("message", {})
            u = msg.get("usage", {}) or {}
            in_sum += int(u.get("input_tokens") or 0)
            out_sum += int(u.get("output_tokens") or 0)
            for b in msg.get("content", []):
                if b.get("type") == "tool_use":
                    tool_calls += 1
                    tool_names.append(b.get("name", ""))
                    if b.get("name") == "repl":
                        repl_used = True
                elif b.get("type") == "text" and b.get("text", "").strip():
                    final_text = b["text"].strip()
    return {
        "case": name, "rc": rc, "rounds": rounds, "tool_calls": tool_calls,
        "tool_names": tool_names, "input_tokens_sum": in_sum,
        "output_tokens_sum": out_sum, "repl_used": repl_used,
        "final_text": final_text[:200], "stderr_tail": stderr[-300:] if rc else "",
    }


def main() -> None:
    key = magpie_key()
    print(f"model={MODEL} via magpie {BASE_URL}（真实 LLM，源码含 repl 接入）")
    a = run_case("A-traditional", PROMPT_A, key)
    b = run_case("B-codemode", PROMPT_B, key)
    print("=" * 70)
    print(f"{'指标':<26}{'A traditional':>18}{'B code-mode':>18}")
    print(f"{'LLM 往返次数':<26}{a['rounds']:>18}{b['rounds']:>18}")
    print(f"{'工具调用数':<26}{a['tool_calls']:>18}{b['tool_calls']:>18}")
    print(f"{'累计输入 tokens':<26}{a['input_tokens_sum']:>18}{b['input_tokens_sum']:>18}")
    print(f"{'累计输出 tokens':<26}{a['output_tokens_sum']:>18}{b['output_tokens_sum']:>18}")
    print(f"{'repl 被使用':<26}{str(a['repl_used']):>18}{str(b['repl_used']):>18}")
    print(f"{'exit code':<26}{a['rc']:>18}{b['rc']:>18}")
    print("-" * 70)
    print("A 工具序列:", " → ".join(a["tool_names"]))
    print("B 工具序列:", " → ".join(b["tool_names"]))
    print("A 汇总:", a["final_text"])
    print("B 汇总:", b["final_text"])
    if a["input_tokens_sum"] and b["input_tokens_sum"]:
        save = 1 - b["input_tokens_sum"] / a["input_tokens_sum"]
        print(f"累计输入 tokens 节省: {save:.1%}")
    if a["stderr_tail"]:
        print("A stderr:", a["stderr_tail"])
    if b["stderr_tail"]:
        print("B stderr:", b["stderr_tail"])
    print("=" * 70)
    Path(os.environ.get("CM_OUT", "/tmp/code_mode_real_result.json")).write_text(
        json.dumps({"A": a, "B": b}, ensure_ascii=False, indent=2)
    )
    print("明细已存 /tmp/code_mode_real_result.json")


if __name__ == "__main__":
    main()
