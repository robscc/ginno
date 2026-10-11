"""Shared constants + process-lifetime mutable state for the subagent
scheduler.

Single-instance by construction: every submodule reaches these through the
package namespace (``_NS``) at call time — there is deliberately no second
binding of any lock / pending-queue / seen-set anywhere in the package."""

from __future__ import annotations

import asyncio

# Hard caps (design §5.3 / contract 5+6). Depth counts subagent layers only
# (0/1/2); a depth-2 session has no spawn_subagent registered at all, so the
# depth check below is defense for sessions built before that rule.
SUBAGENT_MAX_DEPTH = 2
# P3 contract 3: the concurrency cap is now a SETTING (subagent.max_concurrent,
# 1-16, default below), read dynamically on every spawn/validate; the env var
# GINNO_SUBAGENT_MAX overrides with the highest priority (tests). This module
# constant survives only as the documented default.
SUBAGENT_MAX_CONCURRENT = 5
SUBAGENT_MIN_CONCURRENT = 1
SUBAGENT_MAX_CONCURRENT_LIMIT = 16

# One-shot wrap-up instruction for a subagent whose turn died on the LangGraph
# recursion limit (engine.py turn_recursion_limit): salvage a final report from
# the material it already gathered instead of failing with summary_len=0.
_RECURSION_WRAP_TEXT_EN = (
    "[SYSTEM NOTE] Your previous turn was interrupted by the step limit; the "
    "material you gathered is mostly there. Do not call any more tools now "
    "(including web_search / web_fetch / file reads). Immediately output the "
    "final report from the material already collected: conclusions + evidence "
    "(sources/files, numbered references) + an item-by-item check against the "
    "acceptance criteria. For parts the material does not cover, state "
    "honestly that they are \"not covered\" — do not fabricate."
)

_RECURSION_WRAP_TEXT = (
    "【系统提示】你上一轮因步数上限被中断，材料已经收集得差不多了。"
    "现在不要再调用任何工具（包括 web_search / web_fetch / 文件读取），"
    "立即基于已收集的材料输出最终报告：结论 + 证据（来源/文件，编号引用）"
    "+ 与验收标准的逐条对照。材料不足的部分如实写明「未覆盖」，不要编造。"
)

# ---- merged result injection (design open question 3, revised 2026-10-01) ----
# Waking an idle parent once PER child result made the main conversation
# summarize piecemeal in live use (a failed child woke it mid-flight; every
# later finish woke it again, each wake re-restating partial state). Idle-
# parent injections now coalesce per parent into ONE wake turn: flush
# immediately when no live descendants remain (last-result latency is
# unchanged), else after INJECTION_COALESCE_S. A flush that finds the parent
# gone live routes through the steer stash instead (the running channel
# batches at the superstep drain anyway).
_PENDING_INJECTIONS: dict[str, list[dict]] = {}
_INJECTION_FLUSH_TASKS: dict[str, asyncio.Task] = {}
_INJECTION_WAKING: set[str] = set()
INJECTION_COALESCE_S = 10.0

# P3 contract 4 — soft-budget warning threshold (notice event, once per
# session; 软引导，不硬拦). The cumulative-token variant (500k) was removed on
# user feedback 2026-10-01: the notice fired on normal multi-subagent research
# runs and read as noise.
WARN_RUNNING_RATIO = 0.8

# Sessions already warned (per kind). Process-lifetime: a session is warned
# once per runtime run, which matches the "不重复刷" contract.
_WARNED_CONCURRENCY: set[str] = set()

# A final report longer than this gets ONE compression call with the session's
# own model before it is injected into the parent (design §5.5; open question 1
# resolved as: only compress above the threshold).
SUMMARY_MAX_CHARS = 5000

# A spawn holds the limit check + session creation; parallel spawn_subagent
# calls in one message must not both slip under the cap.
_SPAWN_LOCK = asyncio.Lock()

TERMINAL_STATUSES = ("done", "failed", "stopped")
LIVE_STATUSES = ("running", "waiting")

# A verb phrase, deliberately shorter than the main-conversation subject title.
SUBAGENT_TITLE_MAX_CHARS = 16
