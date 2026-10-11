"""Standalone brief + result-envelope formatting (design §5.3/§5.5): the
child's FIRST user message and the <ginno_subagent_result> payloads injected
into the parent. Pure functions — no scheduler state."""

from __future__ import annotations

import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


# Standalone brief (design §5.3) + appendix A.4 (evidence-backed report) +
# A.6 (operator output discipline) — the child's FIRST user message.
_REPORT_FORMAT_EN = """Final report format:
Evidence: first list the evidence supporting your conclusions, numbered — file path:line, command output excerpts, test results. For items without solid evidence, write "no evidence — inference".
Conclusions: state each point, tagging the evidence number it relies on as [n].
Acceptance check: go through each acceptance criterion vs what was actually achieved (met / partial / not met + explanation).
If the goal cannot be completed: state clearly where you are stuck, what you already tried, and what input is still needed."""

_REPORT_FORMAT = """最终报告格式：
证据：先列出支撑结论的依据，编号排列——文件路径:行号、命令输出摘录、测试结果。没有可靠依据的条目写「无依据，系推断」。
结论：逐条陈述，句尾以 [n] 标注所依据的证据编号。
验收对照：逐条列出 acceptance 标准与实际达成情况（达成/部分/未达成 + 说明）。
若目标无法完成：明确说明卡在哪一步、已尝试什么、还需要什么输入。"""

_OUTPUT_DISCIPLINE_EN = """Output discipline:
- The workspace and context folders are inherited from the main conversation — use them directly, don't ask again
- Output must be immediately usable: code lands as-is, conclusions are directly quotable; no "to be filled in" placeholders
- Missing one key piece of information: finish everything else first, then ask your (at most 2) specific questions at the end — don't stop mid-way to ask
- Get straight to the point in reports; no "I will…" style openers"""

_OUTPUT_DISCIPLINE = """输出纪律：
- workspace 与上下文目录已继承自主对话，直接使用，不要重新询问
- 产出必须立即可用：代码可直接落地、结论可直接引用，不留「待补充」占位
- 缺一个关键信息时：先完成其余部分，最后集中提出（最多 2 个）具体问题，不要中途停下来问
- 报告直入主题，不写「我将…」式的开场白"""


def _xml_text(value: str) -> str:
    """XML-escape element TEXT content (goal/约束/验收/persona 都是模型自由文本，
    可能含 < > &）。前端用 DOMParser 解析，实体由浏览器解码——两侧不必手写
    反转义（2026-10-01 结构化简报）。"""
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _xml_attr(value: str) -> str:
    """XML-escape an attribute value. The goal is model-authored free text: an
    unescaped quote would end the attribute early and the frontend's
    parseSubagentResult (blocks.tsx) would truncate it — it unescapes the same
    five entities, so both sides must stay in sync."""
    return (
        (value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def format_subagent_result(
    child_id: str, goal: str, summary: str, acceptance: str = ""
) -> str:
    # Acceptance judging (P2 contract 5, pure prompt side): a non-empty
    # acceptance appends ONE line instructing the parent agent to check the
    # result against each criterion and report a one-line verdict. No new
    # injection protocol — same <ginno_subagent_result> envelope.
    acc = (acceptance or "").strip()
    body = summary
    if acc:
        body += _NS.t(
            f"\nAcceptance criteria: {acc}"
            " — when reporting to the user, first go through the criteria one "
            "by one and give a one-line verdict (met / gap + explanation)",
            f"\n验收标准：{acc}"
            "——请在向用户汇报时先逐条对照该标准给出一行判定（通过/有缺口+说明）",
        )
    return (
        f'<ginno_subagent_result session="{_xml_attr(child_id)}"'
        f' goal="{_xml_attr(goal)}">\n'
        # 正文转义：前端改用 DOMParser 解析信封，正文里若出现裸 < 会被当成标签
        # 吞掉内容（报告是模型自由文本）。实体由浏览器解码，显示不变。
        f"{_xml_text(body)}\n"
        "</ginno_subagent_result>"
    )


def format_subagent_failure(child_id: str, goal: str, error: str) -> str:
    # Failure is reported explicitly and SEPARATELY from a result summary
    # (design §5.7): the error text must never masquerade as the report body.
    return (
        f'<ginno_subagent_result session="{_xml_attr(child_id)}"'
        f' goal="{_xml_attr(goal)}">\n'
        + _NS.t(
            "Status: failed (auto-retries exhausted)\n",
            "状态：失败（自动重试已耗尽）\n",
        )
        + _NS.t(
            f"Error: {_xml_text(error)}\n",
            f"错误：{_xml_text(error)}\n",
        )
        + _NS.t(
            "This is a failure report and contains no result summary; use "
            "list_subagents to inspect, or spawn again.\n",
            "本条是失败上报，不包含结果摘要；可用 list_subagents 查看，或重新 spawn。\n",
        )
        + "</ginno_subagent_result>"
    )


def build_subagent_brief(
    goal: str,
    constraints: str,
    acceptance: str,
    depth: int,
    fork: bool = False,
    persona_body: str = "",
) -> str:
    can_spawn = depth < _NS.SUBAGENT_MAX_DEPTH
    parts = []
    if (persona_body or "").strip():
        # P3 contract 1: a matched registry type prepends its markdown body
        # (persona 补充系统提示) BEFORE the brief envelope.
        parts.append(persona_body.strip())
    # 结构化简报：每段一个标签，前端用 DOMParser 取（不再靠正则切文本），
    # 段名与 spawn_subagent 的参数面对齐（goal/constraints/acceptance + other）。
    other = [
        _NS.t(
            "Working directory and context folders: inherited from the parent session — use them directly.",
            "工作目录与上下文目录：继承父 session，直接使用。",
        ),
        _NS.t(
            f"You are a depth-{depth + 1} subagent; you "
            f"{'may' if can_spawn else 'may NOT'} delegate further subtasks.",
            f"你是第 {depth + 1} 层 subagent，"
            f"{'还可以' if can_spawn else '不可以'}再委派子任务。",
        ),
    ]
    if fork:
        # The ASCII "(fork)" token is a CONTRACT: blocks.tsx detects fork
        # briefs by matching it (or the legacy Chinese phrase for old
        # checkpoints). Keep it in both language variants.
        other.insert(
            0,
            _NS.t(
                "You are a parallel branch (fork) split from the parent conversation; "
                "the full parent context is inherited above; the parent's later "
                "progress is not synced automatically — complete your goal independently.",
                "你是从父对话分出的并行分支（fork），上方已继承父对话的完整上下文；"
                "父对话此后的进展不会自动同步，请独立完成你的目标。",
            ),
        )
    lines = [
        "<ginno_subagent_brief>",
        f"<goal>{_xml_text((goal or '').strip())}</goal>",
        f"<constraints>{_xml_text((constraints or '').strip())}</constraints>",
        f"<acceptance>{_xml_text((acceptance or '').strip())}</acceptance>",
        f"<other>{_xml_text(chr(10).join(other))}</other>",
        "<report_format>",
        _xml_text(_NS.t(_REPORT_FORMAT_EN, _REPORT_FORMAT)),
        "</report_format>",
        "<output_discipline>",
        _xml_text(_NS.t(_OUTPUT_DISCIPLINE_EN, _OUTPUT_DISCIPLINE)),
        "</output_discipline>",
        "</ginno_subagent_brief>",
    ]
    parts.append("\n".join(lines))
    return "\n\n".join(parts)
