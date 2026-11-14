#!/usr/bin/env python3
"""工具 schema 固定开销实测（按需注入评估的离线数据源，零 LLM 调用）。

build_all_tools 全量工具 → convert_to_openai_tool 取真实下发 schema →
tiktoken cl100k_base 计数 → 按功能类分组。回答：每轮全量重放的 tools
schema 占多少 token、哪些类是大头、按需注入能省多少。

复跑：uv run --directory packages/runtime python scripts/schema_overhead_measure.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ginno_runtime.graph import build_all_tools  # noqa: E402

ENC = tiktoken.get_encoding("cl100k_base")

# 功能类分组（按工具名前缀/集合）——用于定位大头与按需注入候选
GROUPS = {
    "file_shell": {"read_file", "write_file", "edit_file", "glob_files", "grep_files", "bash"},
    "browser": {"browser_computer", "browser_read_page", "browser_find", "browser_form_input",
                "browser_navigate", "browser_page_text", "browser_js", "browser_console",
                "browser_network", "browser_file_upload", "browser_resize_window",
                "browser_tabs_context", "browser_tabs_create", "browser_tabs_close",
                "browser_handoff", "browser_stop"},
    "web": {"web_search", "web_fetch"},
    "subagent_worker": {"spawn_subagent", "list_subagents", "delegate_agent", "assign_worker",
                        "watch_worker", "worker_status", "create_subagent_plan", "amend_plan",
                        "cancel_plan", "list_workers"},
    "workflow": {"workflow_list", "workflow_get", "workflow_create", "workflow_run",
                 "workflow_run_status", "workflow_step", "workflow_propose_edit", "workflow_dry_run"},
    "goal": {"goal_get", "goal_create", "goal_update"},
    "skill": {"use_skill", "list_skills", "install_skills", "uninstall_skill"},
    "artifact_render": {"artifact_register", "render_widget", "attach_ref", "parse_document"},
    "misc": {"ask_user", "mcp_trending_render", "mcp_trending_repo_detail", "repl"},
}


def group_of(name: str) -> str:
    for g, members in GROUPS.items():
        if name in members:
            return g
    if name.startswith("mcp_"):
        return "mcp_other"
    return "ungrouped"


def main() -> None:
    tools = build_all_tools(workspace="/tmp")
    rows = []
    total = 0
    for t in tools:
        schema = convert_to_openai_tool(t)
        n = len(ENC.encode(json.dumps(schema, ensure_ascii=False)))
        rows.append((t.name, group_of(t.name), n))
        total += n

    # 按类汇总
    by_group: dict[str, list] = {}
    for name, g, n in rows:
        by_group.setdefault(g, []).append((name, n))

    print("=" * 66)
    print(f"工具 schema 固定开销实测（{len(tools)} 工具，cl100k_base，每轮全量重放）")
    print("=" * 66)
    print(f"{'功能类':<20}{'工具数':>8}{'tokens':>10}{'占比':>9}")
    print("-" * 66)
    for g, items in sorted(by_group.items(), key=lambda kv: -sum(n for _, n in kv[1])):
        gt = sum(n for _, n in items)
        print(f"{g:<20}{len(items):>8}{gt:>10}{gt/total:>8.1%}")
    print("-" * 66)
    print(f"{'合计':<20}{len(tools):>8}{total:>10}{1.0:>8.1%}")
    print()
    # 单工具 top 12（大头）
    print("单工具 schema token Top 12（大头）：")
    for name, g, n in sorted(rows, key=lambda r: -r[2])[:12]:
        print(f"  {n:>6}  {name:<28} [{g}]")
    print()
    # 按需注入潜力：低频类（workflow/goal/subagent/skill/browser）若按需注入可省
    low_freq = {"workflow", "goal", "subagent_worker", "skill", "browser", "artifact_render"}
    saveable = sum(n for _, g, n in rows if g in low_freq)
    core = total - saveable
    print(f"按需注入潜力估算：")
    print(f"  核心常驻（file_shell/web/misc/mcp）≈ {core} tokens")
    print(f"  低频可注入（{'/'.join(sorted(low_freq))}）≈ {saveable} tokens（{saveable/total:.1%}）")
    print("=" * 66)
    Path("/tmp/schema_overhead.json").write_text(json.dumps(
        {"total_tools": len(tools), "total_tokens": total,
         "by_group": {g: {"count": len(i), "tokens": sum(n for _, n in i)} for g, i in by_group.items()},
         "rows": rows}, ensure_ascii=False, indent=2))
    print("明细已存 /tmp/schema_overhead.json")


if __name__ == "__main__":
    main()
