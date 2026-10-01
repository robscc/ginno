"""Subagent lifecycle scheduler (docs/subagent-design.md §5.5-§5.8).

A subagent IS a child session: this module owns everything the plain session
machinery does not already give it —

* spawn: depth check → concurrency cap (5, hard) → child session creation
  (delegates to ``api.sessions.create_session`` so the meta/graph/toolset are
  built exactly like any session) → standalone brief → background turn;
* the STRUCTURAL completion gate (§5.6): a subagent is done only when its own
  turn has ended AND no descendant is still running; otherwise it parks in
  ``waiting`` until the last descendant's result wakes it with a new turn;
* result injection into the parent (§5.5): ``steer_enqueue`` when the parent is
  running, a new system-side turn when it is idle — the same two channels user
  steering uses;
* stop cascade and cascade delete (§5.7/§5.8) over the parent_session_id tree.

Import discipline: this module sits under graph/tools in the import order
(``graph.build_all_tools`` pulls in ``tools.subagent``), so api.stream /
api.sessions are imported LAZILY inside the functions that need them.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from . import paths
from . import server_shared
from .server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _STEER_STASH,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    _push_session_event,
    spawn_bg,
    steer_enqueue,
)
from .session_meta import (
    _find_meta,
    _session_meta_descendants,
    _session_meta_patch,
    _session_meta_remove,
    _session_slug,
    subagent_depth_of,
)

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


def max_concurrent() -> int:
    """The live concurrency cap: settings ``subagent.max_concurrent`` (1-16),
    overridden by ``GINNO_SUBAGENT_MAX`` (highest priority — tests). Read on
    every call so a settings write takes effect without a restart."""
    raw: str | None = os.environ.get("GINNO_SUBAGENT_MAX")
    if raw and raw.strip():
        try:
            return _clamp_concurrent(int(raw.strip()))
        except ValueError:
            _log.warning("subagent_max_env_invalid value=%r", raw)
    try:
        p = paths.settings_path()
        if p.exists():
            import json as _json

            settings = _json.loads(p.read_text() or "{}")
            val = ((settings.get("subagent") or {}) if isinstance(settings, dict) else {}).get(
                "max_concurrent"
            )
            if val is not None:
                return _clamp_concurrent(int(val))
    except (OSError, ValueError, TypeError):
        _log.info("subagent_max_settings_unreadable", exc_info=True)
    return SUBAGENT_MAX_CONCURRENT


def _clamp_concurrent(value: int) -> int:
    return max(SUBAGENT_MIN_CONCURRENT, min(SUBAGENT_MAX_CONCURRENT_LIMIT, int(value)))

# A final report longer than this gets ONE compression call with the session's
# own model before it is injected into the parent (design §5.5; open question 1
# resolved as: only compress above the threshold).
SUMMARY_MAX_CHARS = 5000

# A spawn holds the limit check + session creation; parallel spawn_subagent
# calls in one message must not both slip under the cap.
_SPAWN_LOCK = asyncio.Lock()

TERMINAL_STATUSES = ("done", "failed", "stopped")
LIVE_STATUSES = ("running", "waiting")

# Result-injection format (contract 3). The persisted HumanMessage carries
# additional_kwargs["ginno_subagent_result"] = child_id on BOTH channels
# (steer entry "extra_kwargs" / wake turn "user_extra_kwargs").


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
        body += (
            f"\n验收标准：{acc}"
            "——请在向用户汇报时先逐条对照该标准给出一行判定（通过/有缺口+说明）"
        )
    return (
        f'<ginno_subagent_result session="{_xml_attr(child_id)}"'
        f' goal="{_xml_attr(goal)}">\n'
        f"{body}\n"
        "</ginno_subagent_result>"
    )


def format_subagent_failure(child_id: str, goal: str, error: str) -> str:
    # Failure is reported explicitly and SEPARATELY from a result summary
    # (design §5.7): the error text must never masquerade as the report body.
    return (
        f'<ginno_subagent_result session="{_xml_attr(child_id)}"'
        f' goal="{_xml_attr(goal)}">\n'
        "状态：失败（自动重试已耗尽）\n"
        f"错误：{error}\n"
        "本条是失败上报，不包含结果摘要；可用 list_subagents 查看，或重新 spawn。\n"
        "</ginno_subagent_result>"
    )


# Standalone brief (design §5.3) + appendix A.4 (evidence-backed report) +
# A.6 (operator output discipline) — the child's FIRST user message.
_REPORT_FORMAT = """最终报告格式：
证据：先列出支撑结论的依据，编号排列——文件路径:行号、命令输出摘录、测试结果。没有可靠依据的条目写「无依据，系推断」。
结论：逐条陈述，句尾以 [n] 标注所依据的证据编号。
验收对照：逐条列出 acceptance 标准与实际达成情况（达成/部分/未达成 + 说明）。
若目标无法完成：明确说明卡在哪一步、已尝试什么、还需要什么输入。"""

_OUTPUT_DISCIPLINE = """输出纪律：
- workspace 与上下文目录已继承自主对话，直接使用，不要重新询问
- 产出必须立即可用：代码可直接落地、结论可直接引用，不留「待补充」占位
- 缺一个关键信息时：先完成其余部分，最后集中提出（最多 2 个）具体问题，不要中途停下来问
- 报告直入主题，不写「我将…」式的开场白"""


def build_subagent_brief(
    goal: str,
    constraints: str,
    acceptance: str,
    depth: int,
    fork: bool = False,
    persona_body: str = "",
) -> str:
    can_spawn = depth < SUBAGENT_MAX_DEPTH
    parts = []
    if (persona_body or "").strip():
        # P3 contract 1: a matched registry type prepends its markdown body
        # (persona 补充系统提示) BEFORE the brief envelope.
        parts.append(persona_body.strip())
    lines = ["<ginno_subagent_brief>"]
    if fork:
        lines.append(
            "你是从父对话分出的并行分支（fork），上方已继承父对话的完整上下文；"
            "父对话此后的进展不会自动同步，请独立完成你的目标。"
        )
    lines.extend(
        [
            f"目标：{(goal or '').strip()}",
            f"约束：{(constraints or '').strip() or '（无）'}",
            f"验收标准：{(acceptance or '').strip() or '（无）'}",
            "工作目录与上下文目录：继承父 session，直接使用。",
            f"你是第 {depth + 1} 层 subagent，"
            f"{'还可以' if can_spawn else '不可以'}再委派子任务。",
            "完成后输出最终报告：结论 + 关键产出物路径 + 与验收标准的对照"
            "（证据化格式见下，输出纪律见文末）。",
            "</ginno_subagent_brief>",
        ]
    )
    parts.extend(["\n".join(lines), _REPORT_FORMAT, _OUTPUT_DISCIPLINE])
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# meta / tree helpers
# --------------------------------------------------------------------------- #

def _is_subagent(meta: dict | None) -> bool:
    return bool(meta) and meta.get("type") == "subagent" and bool(meta.get("subagent"))


def _subagent_status(meta: dict | None) -> str:
    return str(((meta or {}).get("subagent") or {}).get("status") or "")


def _live_descendants(slug: str, session_id: str) -> list[dict]:
    return [
        m
        for m in _session_meta_descendants(slug, session_id)
        if _subagent_status(m) in LIVE_STATUSES
    ]


def _running_subagent_metas() -> list[dict]:
    """Every RUNNING subagent across ALL projects (the cap is process-wide).

    The cap is about in-flight TURNS: a ``waiting`` parent holds no turn of
    its own (it is parked until a descendant wakes it), so it must not
    consume a concurrency slot — only ``running`` counts here. LIVE_STATUSES
    stays the completion gate's notion of "has unfinished work"."""
    out: list[dict] = []
    for idx in paths.home().glob("projects/*/sessions/_index.json"):
        from .session_meta import _session_meta_list

        for m in _session_meta_list(idx.parent.parent.name):
            if _is_subagent(m) and _subagent_status(m) == "running":
                out.append(m)
    return out


def reconcile_orphan_subagents() -> int:
    """Startup backstop (design §5.7): at lifespan start no background task of
    the previous process survives — no turn task, no queued wake, no settle
    hook — so every LIVE (running/waiting) subagent meta on disk is a strand.
    Left as-is, a ghost ``running`` meta consumes a concurrency slot forever
    (the cap counts status=="running" metas) and shows a live sidebar dot no
    stop machinery will ever clear (nothing observes an event; only a manual
    user stop would settle it), and a ghost ``waiting`` parent is stranded
    with no descendant left able to wake it.

    Quietly settle each to ``stopped``: meta patch only — no summary, no
    result injection, no waiting-parent wake (the parents are stranded too
    and this same walk settles them, so order does not matter and no wake
    turn may be opened at boot). No sockets are connected yet; the sidebar
    reads the settled metas when the UI connects. Returns the count settled.
    Best-effort like the workflow-run reconciliation it mirrors."""
    from .session_meta import _session_meta_list

    settled = 0
    try:
        indexes = list(paths.home().glob("projects/*/sessions/_index.json"))
    except Exception:
        _log.exception("subagent_reconcile_glob_failed")
        return 0
    for idx in indexes:
        slug = idx.parent.parent.name
        try:
            metas = _session_meta_list(slug)
        except Exception:
            _log.exception("subagent_reconcile_list_failed slug=%s", slug)
            continue
        for m in metas:
            if not _is_subagent(m) or _subagent_status(m) not in LIVE_STATUSES:
                continue
            try:
                sa = dict(m.get("subagent") or {})
                sa["status"] = "stopped"
                _session_meta_patch(slug, m["id"], {"subagent": sa})
                settled += 1
                _log.info(
                    "subagent_orphan_settled session=%s was=%s",
                    m["id"], _subagent_status(m),
                )
            except Exception:
                _log.exception(
                    "subagent_reconcile_patch_failed session=%s", m.get("id"),
                )
    if settled:
        _log.info("subagent_orphans_settled count=%d", settled)
    return settled


def _touch_in_memory(session_id: str, patch: dict) -> None:
    s = _SESSIONS.get(session_id)
    if s:
        s.update(patch)


async def _push_both(parent_id: str | None, child_id: str, event: str, data: dict) -> None:
    """subagent.* events go to the child's own sockets AND every ancestor's:
    a grandchild's status frame must reach the grandparent's sidebar too, or
    the nested status dots there go stale (the sidebar tree renders the whole
    parent_session_id chain, not just direct children)."""
    seen: set[str] = set()
    pid = parent_id
    while pid and pid not in seen:
        seen.add(pid)
        await _push_session_event(pid, event, data)
        found = _find_meta(pid)
        pid = found[0].get("parent_session_id") if found else None
    if child_id and child_id not in seen:
        await _push_session_event(child_id, event, data)


# --------------------------------------------------------------------------- #
# spawn (contract 4/5, design §5.1/§5.3)
# --------------------------------------------------------------------------- #

async def create_subagent(
    parent_session_id: str,
    goal: str,
    constraints: str = "",
    acceptance: str = "",
    origin: str = "agent",
    agent_type: str = "",
    fork: bool = False,
) -> dict:
    """Create + launch one child session. Never raises; ``ok: False`` results
    carry an ``[error]``-style message the tool returns verbatim.

    P3 contract 1/2: ``agent_type`` routes through the type registry (persona
    body prepended to the brief, toolset tightened by the intersected allow
    list, optional model override); ``fork`` seeds the child's initial history
    with a copy of the parent's checkpoint messages (ALL ids freshly minted)
    plus the standalone brief at the top. A fork child itself refuses
    ``fork=True``; fork does NOT change the depth rules."""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal 不能为空"}
    from .api.sessions import _ensure_session  # lazy: cycle

    parent = _SESSIONS.get(parent_session_id) or _ensure_session(parent_session_id)
    if parent is None:
        return {"ok": False, "error": f"未知父会话 {parent_session_id}"}
    slug = parent["project_slug"]
    found = _find_meta(parent_session_id)
    parent_meta = found[0] if found else {}

    depth = subagent_depth_of(parent_meta or parent) + 1
    if depth > SUBAGENT_MAX_DEPTH:
        return {
            "ok": False,
            "error": (
                f"[error] 已达最大嵌套深度（{SUBAGENT_MAX_DEPTH + 1} 层），"
                "不能再委派子任务。请自己完成该工作。"
            ),
        }

    # Type registry (P3 contract 1). Unknown → [error] + the available list,
    # so the model can correct itself in the next call.
    st = None
    if (agent_type or "").strip():
        from .subagent_types import describe_types, get_subagent_type

        st = get_subagent_type(agent_type)
        if st is None:
            avail = "、".join(t["name"] for t in describe_types()) or "（注册表为空）"
            return {
                "ok": False,
                "error": (
                    f"[error] 未知 subagent 类型：{agent_type.strip()}。"
                    f"可用类型：{avail}"
                ),
            }
    # Fork lineage (P3 contract 2): a fork child cannot fork again. P1/P2
    # metas have no ``mode`` field and read as "standard".
    parent_mode = str(
        ((parent_meta.get("subagent") or {}).get("mode")) or "standard"
    )
    if fork and parent_mode == "fork":
        return {
            "ok": False,
            "error": "[error] fork 子代不能再 fork。请使用标准 spawn（不继承上下文）。",
        }

    cap = max_concurrent()
    async with _SPAWN_LOCK:
        running = _running_subagent_metas()
        if len(running) >= cap:
            listing = "\n".join(
                f"- {m['id']} {((m.get('subagent') or {}).get('goal') or '')[:40]}"
                f"（{_subagent_status(m)}）"
                for m in running[:cap]
            )
            return {
                "ok": False,
                "limit": True,
                "error": (
                    f"[error] 并发 subagent 已达上限（{cap}），"
                    f"勿立即重试。当前在跑清单：\n{listing}"
                ),
            }

        from .api.sessions import CreateSessionRequest, create_session  # lazy: cycle

        provider, model_name = parent.get("model_provider"), parent.get("model_name")
        if st is not None:
            from .subagent_types import resolve_type_model

            provider, model_name = resolve_type_model(
                st, (provider, model_name)
            )
        title = goal.replace("\n", " ")[:40]
        req = CreateSessionRequest(
            project_slug=slug,
            workspace=str(parent.get("workspace") or ""),
            agent_id=parent.get("agent_id"),
            title=title,
            type="subagent",
            # Inherit the parent's model config + mounts; the workspace itself
            # stays per-session (create_session supersedes it by design). A
            # matched type may override the model and tightens the toolset.
            provider=provider,
            model=model_name,
            context_folders=list(parent_meta.get("context_folders") or []),
            primary_folder=parent_meta.get("primary_folder") or None,
            parent_session_id=parent_session_id,
            restrict_tools=list(st.tools_allow) if st is not None else [],
            subagent={
                "goal": goal,
                "constraints": constraints or "",
                "acceptance": acceptance or "",
                "origin": origin,
                "status": "running",
                "result_summary": "",
                # P3 contract 2: "standard" | "fork"; absent (P1/P2 legacy
                # metas) reads as standard everywhere.
                "mode": "fork" if fork else "standard",
                "agent_type": st.name if st is not None else "",
            },
        )
        resp = await create_session(req)
        if not resp.get("ok"):
            return {"ok": False, "error": str(resp.get("error") or "子会话创建失败")}
        child_id = resp["id"]

    # Pre-arm the cooperative stop event BEFORE the turn task exists (the same
    # protocol the WS invoke loop uses): a user kill in the spawn window must
    # reach the turn. Consumed + popped by _run_managed_turn.
    stop_evt = _TURN_STOP.setdefault(child_id, asyncio.Event())

    brief = build_subagent_brief(
        goal, constraints, acceptance, depth,
        fork=fork,
        persona_body=st.body if st is not None else "",
    )
    if fork:
        # Seed the fork child's checkpoint: brief at the TOP + the parent's
        # current messages (fresh ids). Failure degrades to a standard spawn
        # (the brief still runs as the first turn) — a fork must never fail
        # the whole spawn.
        try:
            await _seed_fork_history(parent_session_id, child_id, slug, brief)
            first_text = _FORK_START_TEXT
        except Exception:
            _log.exception(
                "subagent_fork_seed_failed parent=%s child=%s",
                parent_session_id, child_id,
            )
            first_text = brief
    else:
        first_text = brief
    await _push_both(
        parent_session_id,
        child_id,
        "subagent.spawned",
        {
            "session_id": child_id,
            "parent_session_id": parent_session_id,
            "goal": goal,
            "constraints": constraints or "",
            "acceptance": acceptance or "",
            "depth": depth,
            "origin": origin,
            "title": title,
            "mode": "fork" if fork else "standard",
            "agent_type": st.name if st is not None else "",
        },
    )
    _log.info(
        "subagent_spawn parent=%s child=%s depth=%d origin=%s mode=%s type=%s goal=%r",
        parent_session_id, child_id, depth, origin,
        "fork" if fork else "standard", st.name if st is not None else "",
        goal[:80],
    )
    spawn_bg(_run_managed_turn(child_id, first_text, None, stop_evt))
    await _maybe_warn_concurrency(parent_session_id)
    return {"ok": True, "session_id": child_id, "title": title, "depth": depth}


# The fork child's first turn: the brief already sits at the top of the seeded
# history, so the turn itself only needs a start instruction.
_FORK_START_TEXT = (
    "这是从父对话分出的并行分支（fork），上方已继承父对话的完整上下文"
    "（含你的 brief）。请直接开始执行目标，完成后按报告格式输出最终报告。"
)


def _fork_copy(m):
    """Deep-faithful copy of one checkpoint message under a FRESH id.

    The id discipline (steering/checkpointer contract): the file checkpointer's
    delta compares message ids only, so any copy into another session's
    history must re-mint every id — a reused id that later diverges would
    silently store nothing. tool_calls / tool_call_id / additional_kwargs are
    preserved (attribution tags, steer markers, image/code trailers all ride
    there), so the child's UI history replays faithfully.

    Strip-chain verdict (P3 contract 2 核查项): the seed keeps FULL fidelity —
    strip_old_images / strip_tool_*_markers are MODEL-VIEW transforms applied
    by agent_node at every turn, so the fork child's model context is bounded
    by the same chain automatically. Stripping in the SEED would instead
    corrupt the child's persisted record (its UI history would lose the image
    payloads / code-panel markers the parent's view still shows)."""
    import copy as _copy
    import uuid as _uuid

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    kw = dict(getattr(m, "additional_kwargs", None) or {})
    if isinstance(m, HumanMessage):
        return HumanMessage(content=m.content, additional_kwargs=kw, id=_fork_id(m))
    if isinstance(m, ToolMessage):
        return ToolMessage(
            content=m.content,
            tool_call_id=getattr(m, "tool_call_id", None),
            name=getattr(m, "name", None),
            additional_kwargs=kw,
            id=_fork_id(m),
        )
    if isinstance(m, AIMessage):
        return AIMessage(
            content=m.content,
            tool_calls=list(getattr(m, "tool_calls", None) or []),
            additional_kwargs=kw,
            id=_fork_id(m),
        )
    out = _copy.deepcopy(m)
    try:
        out.id = f"fork-{_uuid.uuid4().hex[:12]}"
    except Exception:
        pass
    return out


def _fork_id(_m) -> str:
    import uuid as _uuid

    return f"fork-{_uuid.uuid4().hex[:12]}"


def _heal_fork_tail(copies: list) -> int:
    """Answer dangling tool_calls at the tail of the COPIED history.

    A fork always runs from inside the parent's LIVE turn (the spawn tool
    executes in the parent's tools node), and checkpoints commit per
    superstep — so the parent checkpoint being copied ends with the AIMessage
    whose tool_calls (spawn_subagent's own, plus any parallel calls in the
    same batch) have NO ToolMessage answers yet; the batch commits only when
    the tools node returns. Copied as-is, the child's first model request
    forwards a tool_use with no tool_result and the provider API 400s every
    turn of the fork child — the exact shape ``_heal_interrupted_turn``
    documents and repairs for a stopped turn (engine.py). This appends the
    same "(interrupted)" placeholders to the COPY only: the parent's own turn
    continues and answers those calls for real, so the parent's record and
    the child's divergence here is correct (the child inherits context, not
    the parent's in-flight work).

    Returns the number of placeholder ToolMessages appended. Only the TAIL
    can dangle: mid-history tool batches are complete at every committed
    superstep (and a stopped turn's dangling tail was already healed in the
    parent before any later turn ran)."""
    from langchain_core.messages import AIMessage, ToolMessage

    if not copies or not isinstance(copies[-1], AIMessage):
        return 0
    added = 0
    for tc in getattr(copies[-1], "tool_calls", None) or []:
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tc_id:
            continue
        tc_name = (
            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        ) or ""
        copies.append(
            ToolMessage(
                content="(interrupted)",
                tool_call_id=tc_id,
                name=tc_name,
                # Fresh id: the checkpointer's delta is id-keyed; a reused or
                # missing id would corrupt the child's delta chain.
                id=_fork_id(None),
            )
        )
        added += 1
    if added:
        _log.info(
            "subagent_fork_tail_healed placeholders=%d", added,
        )
    return added


async def _seed_fork_history(
    parent_session_id: str, child_id: str, slug: str, brief_text: str
) -> int:
    """Copy the parent's current checkpoint messages into the child's fresh
    checkpoint (via the child graph's own aupdate_state — the same rewrite
    path compaction uses, so the checkpoint shape is guaranteed correct),
    prefixed by the standalone brief. Returns the number of copied messages."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from .checkpointer import FileCheckpointer

    tup = await FileCheckpointer(slug).aget_tuple(
        {"configurable": {"thread_id": parent_session_id}}
    )
    msgs = (
        ((tup.checkpoint.get("channel_values") or {}).get("messages") or [])
        if tup
        else []
    )
    copies = [
        _fork_copy(m) for m in msgs if not isinstance(m, SystemMessage)
    ]
    # The parent is mid-turn (the fork runs inside its tools node): heal the
    # dangling tool_call tail BEFORE seeding, or the child's first turn 400s.
    _heal_fork_tail(copies)
    brief_msg = HumanMessage(
        content=brief_text,
        # New id, same discipline as every steer/result injection.
        id=f"fork-brief-{uuid.uuid4().hex[:10]}",
        additional_kwargs={"ginno_subagent_fork": parent_session_id},
    )
    child = _SESSIONS.get(child_id)
    graph = (child or {}).get("graph")
    if graph is None:
        raise RuntimeError(f"fork 子会话 {child_id} 无可用 graph")
    await graph.aupdate_state(
        {
            "configurable": {
                "thread_id": child_id,
                "project_slug": slug,
                "agent_id": (child or {}).get("agent_id"),
            }
        },
        {"messages": [brief_msg, *copies]},
        as_node="agent",
    )
    _log.info(
        "subagent_fork_seeded parent=%s child=%s messages=%d",
        parent_session_id, child_id, len(copies),
    )
    return len(copies)


# --------------------------------------------------------------------------- #
# soft-budget warnings (P3 contract 4) — notice event, once per session
# --------------------------------------------------------------------------- #

def _concurrency_warn_floor(cap: int) -> int:
    """running ≥ ceil(cap * 80%) fires the once-per-session warning."""
    import math

    return max(1, math.ceil(cap * WARN_RUNNING_RATIO))


async def _warn_once(session_id: str, seen: set[str], message: str) -> bool:
    if not session_id or session_id in seen:
        return False
    seen.add(session_id)
    try:
        await _push_session_event(session_id, "notice", {"message": message})
    except Exception:
        _log.exception("subagent_warn_push_failed session=%s", session_id)
        return True
    return True


async def _maybe_warn_concurrency(owner_session_id: str) -> bool:
    """Trigger 1: running count reached 80% of the (live) cap. Keyed by the
    OWNING session — each session is told once per runtime run."""
    cap = max_concurrent()
    running = len(_running_subagent_metas())
    floor = _concurrency_warn_floor(cap)
    if running < floor:
        return False
    return await _warn_once(
        owner_session_id,
        _WARNED_CONCURRENCY,
        f"提示：当前并发运行中的 subagent 已达 {running} 个"
        f"（达到上限 {cap} 的 80%）。请留意拆分粒度，勿继续大量并发委派。",
    )


# --------------------------------------------------------------------------- #
# turn runner (background; pattern: _run_goal_turn / _WF_RUN_TASKS)
# --------------------------------------------------------------------------- #

async def _run_managed_turn(
    session_id: str,
    text: str,
    extra_kwargs: dict | None,
    stop_evt: asyncio.Event | None = None,
) -> None:
    """Run ONE headless turn for a scheduler-managed session (child brief, or a
    result-injection wake into an idle parent). The caller must NOT hold the
    turn lock. Afterwards the completion gate runs if the session is a subagent."""
    from .api.sessions import _ensure_session, _first_agent_id  # lazy: cycle
    from .api.stream import _run_stream  # lazy: cycle

    session = _SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        _log.error("subagent_turn_missing_session session=%s", session_id)
        _TURN_STOP.pop(session_id, None)
        return
    if stop_evt is None:
        stop_evt = _TURN_STOP.setdefault(session_id, asyncio.Event())
    if stop_evt.is_set():
        # Killed in the spawn window: never run, settle as stopped.
        _TURN_STOP.pop(session_id, None)
        await on_turn_stopped(session_id)
        return
    turn_id = str(uuid.uuid4())
    agent_id = session.get("agent_id") or _first_agent_id()
    slug = session["project_slug"]
    # Distinguishable from user chat in the usage log.
    _found = _find_meta(session_id)
    usage_source = "subagent" if _is_subagent(_found[0] if _found else session) else "chat"
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
        }
    }
    _RUNNING_TURNS[session_id] = turn_id
    try:
        async with _turn_lock_of(session_id):
            await _run_stream(
                None, session["graph"], config, text, session, agent_id,
                user_extra_kwargs=extra_kwargs,
            )
    finally:
        if _RUNNING_TURNS.get(session_id) == turn_id and session_id not in _PENDING_RESUME:
            _RUNNING_TURNS.pop(session_id, None)
        _TURN_STOP.pop(session_id, None)
        # A turn parked at a permission/ask_user interrupt settles later —
        # via the resume path or the parked-stop path (stream.py hooks).
        if session_id not in _PENDING_RESUME:
            try:
                await on_turn_settled(session_id, stop_evt, turn_id)
            except Exception:
                _log.exception("subagent_settle_failed session=%s", session_id)


def _turn_lock_of(session_id: str):
    from .server_shared import _turn_lock

    return _turn_lock(session_id)


# --------------------------------------------------------------------------- #
# completion gate (contract 6, design §5.6)
# --------------------------------------------------------------------------- #

def _is_subagent_injection(entry: dict) -> bool:
    """A steer-stash entry the SCHEDULER enqueued (not a user steer): it carries
    the ``ginno_subagent_result`` marker in ``extra_kwargs`` and non-empty text.
    Only these are reconciled on redelivery — ordinary user steers keep the
    frontend's own turn-end resend semantics (steering-design §3.3)."""
    return bool((entry.get("extra_kwargs") or {}).get("ginno_subagent_result")) and bool(
        (entry.get("text") or "").strip()
    )


def reconcile_stash_injections(session_id: str) -> int:
    """Redeliver UNABSORBED subagent result injections through the wake-turn
    channel (major-1 reconciliation).

    A result injected into a RUNNING parent rides the steer stash — but the
    stash is cleared at the end of every turn segment regardless of absorption,
    on the "the client re-sends what it never saw acknowledged" contract. The
    scheduler never re-sends, so an injection that lands after the parent's
    LAST agent superstep (final reply already streaming) — or into a stash the
    user's stop of a parked turn drops (_stop_parked_turn) — would be lost
    forever. This pulls the tagged entries out of the stash and wakes the
    parent with each one; idempotent (entries leave the stash when handed to
    the wake channel), so calling it from both the turn-segment finally and the
    settle hook is safe.

    Called synchronously BEFORE the generic steer_clear at those two sites.
    """
    stash = _STEER_STASH.get(session_id) or []
    tagged = [e for e in stash if _is_subagent_injection(e)]
    if not tagged:
        return 0
    _STEER_STASH[session_id] = [e for e in stash if not _is_subagent_injection(e)]
    if not _STEER_STASH[session_id]:
        _STEER_STASH.pop(session_id, None)
    for entry in tagged:
        child_id = (entry.get("extra_kwargs") or {}).get("ginno_subagent_result")
        _log.info(
            "subagent_result_reconciled parent=%s child=%s", session_id, child_id
        )
        spawn_bg(
            _wake_parent_turn(session_id, entry["text"], entry.get("extra_kwargs"))
        )
    return len(tagged)


async def on_turn_settled(
    session_id: str, stop_evt: asyncio.Event | None = None, turn_id: str = ""
) -> None:
    """The gate: called after ANY turn of a session settles (scheduler runner,
    WS invoke job, resume job, checkpoint-retry job). No-op unless the session
    is a non-terminal subagent with nothing live.

    stop_evt is the session's cooperative-stop Event object captured before the
    turn's cleanup popped it — a set event means the USER stopped the turn, and
    a user-killed subagent is "stopped" and injects nothing (design §5.7).
    """
    if not session_id or session_id in _PENDING_RESUME:
        return
    task = _TURN_TASKS.get(session_id)
    if task is not None and not task.done() and task is not asyncio.current_task():
        return  # a new segment is already running; it settles for itself
    if session_id in _RUNNING_TURNS:
        return
    # Result injections that missed their absorption window (the turn's last
    # agent superstep had already drained when they arrived) would be dropped
    # by the segment's steer_clear — redeliver them before anything else. The
    # redelivery itself may open a wake turn; the gate below then no-ops via
    # the _RUNNING_TURNS guard on the next settle.
    try:
        reconcile_stash_injections(session_id)
    except Exception:
        _log.exception("subagent_reconcile_failed session=%s", session_id)
    s = _SESSIONS.get(session_id)
    # Cheap short-circuit for main conversations (no disk read). NOTE: the
    # in-memory entry carries ``type`` but not the ``subagent`` payload — the
    # disk meta below is the authority for the subagent fields.
    if s is not None and s.get("type") != "subagent":
        return
    found = _find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _is_subagent(meta) or _subagent_status(meta) in TERMINAL_STATUSES:
        return
    # Settled: drop the stop event so a stale set can never kill the wake turn.
    _TURN_STOP.pop(session_id, None)

    if stop_evt is not None and stop_evt.is_set():
        await _finalize_subagent(slug, meta, "stopped")
        return
    err = None
    if turn_id:
        from .api.sessions import _turn_last_error  # lazy: cycle

        err = _turn_last_error(session_id, turn_id)
    if err is not None:
        # A recursion-limited subagent gets ONE wrap-up continuation instead of
        # an empty-handed failure: the child already did the work, it just never
        # got to write the final report. The continuation instructs it to answer
        # from gathered material with no tool calls; its own settle then
        # finalizes done. Guarded once per subagent via the meta flag (a
        # disobedient wrap-up that hits the limit again fails for real).
        _err_text = str(err.get("message") if isinstance(err, dict) else err or "")
        _sub = dict(meta.get("subagent") or {})
        if "GraphRecursionError" in _err_text and not _sub.get("recursion_wrapped"):
            _sub["recursion_wrapped"] = True
            _session_meta_patch(slug, session_id, {"subagent": _sub})
            _log.info(
                "subagent_recursion_wrap session=%s parent=%s", session_id, meta.get("parent_session_id")
            )
            _TURN_STOP.pop(session_id, None)
            spawn_bg(
                _run_managed_turn(
                    session_id,
                    _RECURSION_WRAP_TEXT,
                    {"ginno_system_note": "recursion-wrap"},
                    asyncio.Event(),
                )
            )
            return
        # AUTO_RETRY exhausted upstream; the error is NOT mixed into any summary
        # (design §5.7) — it rides the failure report only.
        await _finalize_subagent(slug, meta, "failed", error=err)
        return
    if _live_descendants(slug, session_id):
        await _mark_waiting(slug, meta)
        return
    await _finalize_subagent(slug, meta, "done")


async def on_turn_stopped(session_id: str) -> None:
    """Explicit user-stop finalize (parked-turn stop path in stream.py)."""
    found = _find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _is_subagent(meta) or _subagent_status(meta) in TERMINAL_STATUSES:
        return
    _TURN_STOP.pop(session_id, None)
    await _finalize_subagent(slug, meta, "stopped")


async def on_stop_if_waiting(session_id: str) -> None:
    """User pressed stop on a WAITING subagent (no live turn of its own): mark
    it stopped; the descendants were cascade-stopped by stop_descendants.

    minor-C: docstring-promised preconditions are now ENFORCED, under the turn
    lock. The window between a turn's normal completion and its settle hook
    (_TURN_TASKS/_RUNNING_TURNS already popped) used to let a stop that landed
    there finalize the just-succeeded turn as "stopped" instead of "done" —
    swallowing its upward injection. Holding the lock serializes against every
    scheduler-managed turn AND its settle; the live-turn guard + the strict
    ``waiting`` status re-read also refuse the WS-path settle (spawned outside
    the lock), where the meta still reads "running"."""
    async with _turn_lock_of(session_id):
        task = _TURN_TASKS.get(session_id)
        if session_id in _RUNNING_TURNS or (task is not None and not task.done()):
            return  # a live turn exists: its own machinery settles it stopped
        found = _find_meta(session_id)
        if not found:
            return
        meta, slug = found
        if not _is_subagent(meta) or _subagent_status(meta) != "waiting":
            return  # running (settle pending) or already terminal: not ours
        await _finalize_subagent(slug, meta, "stopped")


async def revive_failed_for_retry(session_id: str) -> None:
    """User hit retry on a FAILED subagent's error card (minor-6): the failed
    attempt already finalized the meta terminal ("failed"), and the completion
    gate skips terminal metas — without reviving it first, a successful retry
    would settle into a gate no-op and its result would never reach the parent.
    Flip "failed" → "running" so the retry's settle re-runs the gate."""
    found = _find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _is_subagent(meta) or _subagent_status(meta) != "failed":
        return
    sa = dict(meta.get("subagent") or {})
    sa["status"] = "running"
    _session_meta_patch(slug, session_id, {"subagent": sa})
    _touch_in_memory(session_id, {"subagent": sa})
    await _push_both(
        meta.get("parent_session_id"),
        session_id,
        "subagent.status",
        {
            "session_id": session_id,
            "parent_session_id": meta.get("parent_session_id"),
            "status": "running",
        },
    )
    _log.info("subagent_retry_revived session=%s", session_id)


async def _mark_waiting(slug: str, meta: dict) -> None:
    sa = dict(meta.get("subagent") or {})
    if sa.get("status") == "waiting":
        return
    sa["status"] = "waiting"
    _session_meta_patch(slug, meta["id"], {"subagent": sa})
    _touch_in_memory(meta["id"], {"subagent": sa})
    await _push_both(
        meta.get("parent_session_id"),
        meta["id"],
        "subagent.status",
        {
            "session_id": meta["id"],
            "parent_session_id": meta.get("parent_session_id"),
            "status": "waiting",
        },
    )
    _log.info("subagent_waiting session=%s", meta["id"])


# Session ids with a finalize IN FLIGHT. The terminal check ("is the meta
# already terminal?") happens BEFORE the summary work, and a ``done`` finalize
# can suspend for seconds on the compression LLM call — long enough for a
# second gate invocation on the same session (e.g. a user invoke into the
# session whose wake turn just freed both busy registries) to pass the same
# check and finalize again: double compression, double broadcast, and the SAME
# result injected into the ancestor twice. First-wins; the loser returns (its
# status intent is already covered — see the note at the latch).
_FINALIZING: set[str] = set()


async def _finalize_subagent(
    slug: str, meta: dict, status: str, error: str | None = None
) -> None:
    """Write the terminal status (+ summary when done), broadcast, and inject
    into the parent. Summary + upward injection happen ONCE, here (§5.6)."""
    child_id = meta["id"]
    if child_id in _FINALIZING:
        _log.info(
            "subagent_finalize_skipped_inflight session=%s attempted=%s",
            child_id, status,
        )
        return
    _FINALIZING.add(child_id)
    try:
        await _finalize_subagent_locked(slug, meta, status, error)
    finally:
        _FINALIZING.discard(child_id)


async def _finalize_subagent_locked(
    slug: str, meta: dict, status: str, error: str | None = None
) -> None:
    child_id = meta["id"]
    sa = dict(meta.get("subagent") or {})
    parent_id = meta.get("parent_session_id")
    report = ""
    summary = ""
    if status == "done":
        report = await _final_report_text(slug, child_id)
        summary = await _maybe_compress_summary(report, meta)
    sa["status"] = status
    sa["result_summary"] = summary
    _session_meta_patch(slug, child_id, {"subagent": sa})
    _touch_in_memory(child_id, {"subagent": sa})
    payload: dict = {
        "session_id": child_id,
        "parent_session_id": parent_id,
        "status": status,
    }
    if status == "done":
        payload["result_summary"] = summary
    if status == "failed" and error:
        payload["error"] = error
    await _push_both(parent_id, child_id, "subagent.status", payload)
    _log.info(
        "subagent_finalized session=%s status=%s parent=%s summary_len=%d",
        child_id, status, parent_id, len(summary),
    )
    # LLM title (P2 contract 6): after the FIRST completed turn, one auxiliary
    # call with the session's own model replaces the goal[:40] placeholder with
    # a short verb-phrase title (≤16 chars), refreshed through the existing
    # ``session_title`` event. Fire-and-forget, independent of the upward
    # injection (a subagent whose parent is already terminal still gets its
    # title); any failure keeps the P1 goal[:40] placeholder.
    if status == "done" and not sa.get("titled"):
        spawn_bg(_gen_subagent_title(child_id, report))

    if status == "stopped":
        # User kill: no result injection (design §5.7). But a WAITING ancestor
        # whose whole subtree is now dead must be woken to wrap up (§5.6).
        await _reevaluate_waiting_parent(parent_id)
        return
    if not parent_id:
        return
    # A terminal parent must never be injected (design §5.6: 被级联停止的子代
    # 不向已停止的父注入结果). Without this gate a descendant finishing after
    # its parent was cascade-stopped would wake the dead session and burn a
    # turn on it. Main conversations carry no subagent status and always pass.
    pfound = _find_meta(parent_id)
    if pfound and _is_subagent(pfound[0]) and _subagent_status(pfound[0]) in TERMINAL_STATUSES:
        _log.info(
            "subagent_inject_skipped_terminal_parent parent=%s child=%s status=%s",
            parent_id, child_id, _subagent_status(pfound[0]),
        )
        return
    goal = str(sa.get("goal") or "")
    if status == "failed":
        text = format_subagent_failure(child_id, goal, error or "未知错误")
    else:
        text = format_subagent_result(
            child_id, goal, summary, str(sa.get("acceptance") or "")
        )
    await _inject_result(parent_id, text, child_id)


async def _inject_result(parent_id: str, text: str, child_id: str) -> None:
    """Channel pick (§5.5): running parent → steer stash; idle parent → new
    turn. Defense in depth for the terminal-parent rule above: a terminal
    parent is idle by definition, so without its own check the wake branch
    would open a turn on a stopped/failed/done session."""
    pfound = _find_meta(parent_id)
    if pfound and _is_subagent(pfound[0]) and _subagent_status(pfound[0]) in TERMINAL_STATUSES:
        _log.warning(
            "subagent_inject_refused_terminal_parent parent=%s child=%s",
            parent_id, child_id,
        )
        return
    task = _TURN_TASKS.get(parent_id)
    parent_live = parent_id in _RUNNING_TURNS or (
        task is not None and not task.done()
    )
    if parent_live:
        steer_enqueue(
            parent_id,
            {
                # New id, never a reused turn id (checkpointer delta is id-keyed).
                "steer_id": f"subagent-{child_id}-{uuid.uuid4().hex[:8]}",
                "turn_id": _RUNNING_TURNS.get(parent_id) or "",
                "text": text,
                "injected_at": time.time(),
                # Contract 3: the persisted HumanMessage carries
                # additional_kwargs["ginno_subagent_result"] = child_id
                # (merged by steer_messages alongside ginno_steer).
                "extra_kwargs": {"ginno_subagent_result": child_id},
            },
        )
        _log.info("subagent_result_steer parent=%s child=%s", parent_id, child_id)
    else:
        _queue_injection(parent_id, text, child_id)


def _queue_injection(parent_id: str, text: str, child_id: str) -> None:
    """Idle-parent result: coalesce (see the merged-injection note above)."""
    _PENDING_INJECTIONS.setdefault(parent_id, []).append(
        {"text": text, "child_id": child_id}
    )
    if parent_id in _INJECTION_FLUSH_TASKS:
        return  # already scheduled — this entry rides the pending flush
    pfound = _find_meta(parent_id)
    if not pfound:
        _PENDING_INJECTIONS.pop(parent_id, None)
        return
    if parent_id in _INJECTION_WAKING:
        # A flush's wake is lifting off (its lock section hasn't registered
        # _RUNNING_TURNS yet). Don't race it with a second immediate wake —
        # schedule the timer; by then the parent is live and the flush steers.
        _INJECTION_FLUSH_TASKS[parent_id] = spawn_bg(
            _injection_flush_timer(parent_id)
        )
        return
    if not _live_descendants(pfound[1], parent_id):
        spawn_bg(_flush_injections(parent_id))
        return
    _INJECTION_FLUSH_TASKS[parent_id] = spawn_bg(_injection_flush_timer(parent_id))


async def _injection_flush_timer(parent_id: str) -> None:
    try:
        await asyncio.sleep(INJECTION_COALESCE_S)
    finally:
        _INJECTION_FLUSH_TASKS.pop(parent_id, None)
    await _flush_injections(parent_id)


async def _flush_injections(parent_id: str) -> None:
    if parent_id in _INJECTION_WAKING:
        if parent_id not in _INJECTION_FLUSH_TASKS:
            _INJECTION_FLUSH_TASKS[parent_id] = spawn_bg(
                _injection_flush_timer(parent_id)
            )
        return
    entries = _PENDING_INJECTIONS.pop(parent_id, [])
    if not entries:
        return
    pfound = _find_meta(parent_id)
    if not pfound:
        _log.info("subagent_inject_dropped_no_meta parent=%s", parent_id)
        return
    meta = pfound[0]
    if _is_subagent(meta) and _subagent_status(meta) in TERMINAL_STATUSES:
        _log.warning(
            "subagent_inject_refused_terminal_parent parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        return
    task = _TURN_TASKS.get(parent_id)
    parent_live = parent_id in _RUNNING_TURNS or (
        task is not None and not task.done()
    )
    if parent_live:
        # The parent went live while the queue waited (user spoke / an
        # earlier wake won): same shape as _inject_result's running branch,
        # one stash entry each — the superstep drain batches them.
        for e in entries:
            steer_enqueue(
                parent_id,
                {
                    "steer_id": f"subagent-{e['child_id']}-{uuid.uuid4().hex[:8]}",
                    "turn_id": _RUNNING_TURNS.get(parent_id) or "",
                    "text": e["text"],
                    "injected_at": time.time(),
                    "extra_kwargs": {"ginno_subagent_result": e["child_id"]},
                },
            )
        _log.info(
            "subagent_result_steer parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        return
    texts = [e["text"] for e in entries]
    ids = ",".join(e["child_id"] for e in entries)
    merged = texts[0] if len(texts) == 1 else (
        f"以下是 {len(texts)} 个子代理的回传（结果与失败报告）：\n\n"
        + "\n\n".join(texts)
    )
    _log.info(
        "subagent_result_wake parent=%s children=%s merged=%d",
        parent_id, ids, len(entries),
    )
    _INJECTION_WAKING.add(parent_id)
    try:
        t = spawn_bg(_wake_parent_turn(parent_id, merged, {"ginno_subagent_result": ids}))

        def _wake_done(_f: Any, pid: str = parent_id) -> None:
            _INJECTION_WAKING.discard(pid)

        t.add_done_callback(_wake_done)
    except Exception:
        _INJECTION_WAKING.discard(parent_id)
        raise


async def _wake_parent_turn(
    parent_id: str, text: str, extra_kwargs: dict | None
) -> None:
    """Open a NEW turn on an idle parent carrying the injected message."""
    from .api.sessions import _ensure_session  # lazy: cycle

    # Never wake a terminal subagent (design §5.6): a stale wake racing a
    # cascade stop must not resurrect a stopped session to burn a turn.
    _wfound = _find_meta(parent_id)
    if _wfound and _is_subagent(_wfound[0]) and _subagent_status(_wfound[0]) in TERMINAL_STATUSES:
        _log.warning("subagent_wake_refused_terminal_parent parent=%s", parent_id)
        return
    parent = _SESSIONS.get(parent_id) or _ensure_session(parent_id)
    if parent is None:
        _log.warning("subagent_wake_no_parent parent=%s", parent_id)
        return
    async with _turn_lock_of(parent_id):
        # major-A: the pre-lock check raced every other finalizer — two children
        # of one waiting parent each judge the parent idle before the other's
        # wake flips anything, and the SECOND wake only reaches the lock after
        # the first wake's settle has already finalized the parent done. Re-read
        # the meta UNDER the lock and drop any delivery to a parent that is no
        # longer live (terminal, or a subagent status that is neither running
        # nor waiting): running it would burn a full ghost turn and re-inject
        # the same result into the grandparent.
        _lfound = _find_meta(parent_id)
        if _lfound and _is_subagent(_lfound[0]) and _subagent_status(_lfound[0]) not in LIVE_STATUSES:
            _log.warning(
                "subagent_wake_refused_nonlive_parent parent=%s status=%s",
                parent_id, _subagent_status(_lfound[0]),
            )
            return
        # major-B (same check-then-act root): a user stop that landed while
        # this wake queued on the lock must never be answered with a fresh
        # turn — it would run ahead of the stopped finalize and swallow it.
        _preset = _TURN_STOP.get(parent_id)
        if _preset is not None and _preset.is_set():
            _log.info("subagent_wake_refused_stopped_parent parent=%s", parent_id)
            _TURN_STOP.pop(parent_id, None)
            return
        task = _TURN_TASKS.get(parent_id)
        if parent_id in _RUNNING_TURNS or parent_id in _PENDING_RESUME or (
            task is not None and not task.done()
        ):
            # Raced busy: the steer channel still delivers it this turn.
            steer_enqueue(
                parent_id,
                {
                    "steer_id": f"subagent-wake-{uuid.uuid4().hex[:8]}",
                    "turn_id": _RUNNING_TURNS.get(parent_id) or "",
                    "text": text,
                    "injected_at": time.time(),
                    "extra_kwargs": extra_kwargs,
                },
            )
            return
        # A waiting subagent waking up is running again (contract 6). Register a
        # provisional busy marker BEFORE the flip broadcast: between the checks
        # above and _run_managed_turn_locked's own registration sit the flip's
        # awaits (the WS push), and a user stop landing there read a session
        # that looked IDLE — the WS liveness gate set no event (stop swallowed,
        # the full wake turn ran against it) and a cascade direct-finalized the
        # subagent "stopped" out from under the wake, which then still opened a
        # ghost turn on the dead session. With the marker down, both stop paths
        # arm the event, and the re-check right after the flip refuses.
        _wake_tid = f"wake-{uuid.uuid4().hex[:8]}"
        _RUNNING_TURNS[parent_id] = _wake_tid
        flipped = False
        try:
            found = _find_meta(parent_id)
            if found and _subagent_status(found[0]) == "waiting":
                sa = dict(found[0].get("subagent") or {})
                sa["status"] = "running"
                _session_meta_patch(found[1], parent_id, {"subagent": sa})
                _touch_in_memory(parent_id, {"subagent": sa})
                await _push_both(
                    found[0].get("parent_session_id"),
                    parent_id,
                    "subagent.status",
                    {
                        "session_id": parent_id,
                        "parent_session_id": found[0].get("parent_session_id"),
                        "status": "running",
                    },
                )
                flipped = True
            stop_evt = _TURN_STOP.setdefault(parent_id, asyncio.Event())
            if stop_evt.is_set():
                # major-B: set in the flip window above (cascade stop found the
                # existing entry) — same refusal as the pre-set check. A session
                # we JUST flipped to running must not stay stranded mid-state:
                # the stop that landed settles it "stopped" here.
                _log.info("subagent_wake_refused_stopped_parent parent=%s", parent_id)
                _TURN_STOP.pop(parent_id, None)
                if flipped:
                    await on_turn_stopped(parent_id)
                return
            try:
                await _run_managed_turn_locked(parent_id, text, extra_kwargs, stop_evt)
            except Exception:
                _log.exception("subagent_wake_turn_failed parent=%s", parent_id)
        finally:
            if _RUNNING_TURNS.get(parent_id) == _wake_tid:
                _RUNNING_TURNS.pop(parent_id, None)


async def _run_managed_turn_locked(
    session_id: str, text: str, extra_kwargs: dict | None, stop_evt: asyncio.Event
) -> None:
    """_run_managed_turn for a caller already holding the turn lock."""
    from .api.sessions import _ensure_session, _first_agent_id  # lazy: cycle
    from .api.stream import _run_stream

    session = _SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        _TURN_STOP.pop(session_id, None)
        return
    turn_id = str(uuid.uuid4())
    agent_id = session.get("agent_id") or _first_agent_id()
    # The wake channel also serves MAIN conversations (an idle parent waking to
    # absorb a result): only a subagent session's usage is tagged "subagent"
    # (same rule as _run_managed_turn).
    _found = _find_meta(session_id)
    usage_source = "subagent" if _is_subagent(_found[0] if _found else session) else "chat"
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": session["project_slug"],
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
        }
    }
    _RUNNING_TURNS[session_id] = turn_id
    try:
        await _run_stream(
            None, session["graph"], config, text, session, agent_id,
            user_extra_kwargs=extra_kwargs,
        )
    finally:
        if _RUNNING_TURNS.get(session_id) == turn_id and session_id not in _PENDING_RESUME:
            _RUNNING_TURNS.pop(session_id, None)
        _TURN_STOP.pop(session_id, None)
        if session_id not in _PENDING_RESUME:
            try:
                await on_turn_settled(session_id, stop_evt, turn_id)
            except Exception:
                _log.exception("subagent_settle_failed session=%s", session_id)


async def _reevaluate_waiting_parent(parent_id: str | None) -> None:
    """Anti-starvation wake (§5.6): a waiting subagent with NO live descendant
    left gets one wrap-up message — no one else would inject into it again.

    Mixed terminal subtrees count (major-2): C1 done + C2 stopped leaves no one
    to wake the parent, because a stopped child injects nothing and the done
    child's own wake already happened while C2 was still running. The only
    skip is ALL-done/failed: every one of those finalizers injected its own
    result wake, so the parent is already covered."""
    if not parent_id:
        return
    found = _find_meta(parent_id)
    if not found:
        return
    meta, slug = found
    if _subagent_status(meta) != "waiting":
        return
    descendants = _session_meta_descendants(slug, parent_id)
    if any(_subagent_status(d) in LIVE_STATUSES for d in descendants):
        return
    if descendants and all(
        _subagent_status(d) in ("done", "failed") for d in descendants
    ):
        return  # each finalizer already injected its own wake
    stopped = sum(1 for d in descendants if _subagent_status(d) == "stopped")
    _log.info(
        "subagent_children_all_settled parent=%s stopped=%d total=%d",
        parent_id, stopped, len(descendants),
    )
    wrap_up = (
        "你的子任务均已被用户停止。请直接收尾：汇报目前已完成的部分与剩余风险。"
        if not descendants or stopped == len(descendants)
        else "你的部分子任务已被用户停止，其余已完成。请直接收尾：整合已完成子任务的结果，并说明被停止的部分。"
    )
    # Fold any queued result injections into this wrap-up wake: one turn, one
    # message (the merged-injection queue owns idle wakes for results).
    entries = _PENDING_INJECTIONS.pop(parent_id, [])
    if entries:
        _t = _INJECTION_FLUSH_TASKS.pop(parent_id, None)
        if _t is not None:
            _t.cancel()
        _log.info(
            "subagent_inject_merged_into_wrapup parent=%s children=%s",
            parent_id, ",".join(e["child_id"] for e in entries),
        )
        wrap_up = "\n\n".join([e["text"] for e in entries] + [wrap_up])
    spawn_bg(
        _wake_parent_turn(
            parent_id,
            wrap_up,
            {"ginno_subagent_result": "stopped-children"},
        )
    )


# --------------------------------------------------------------------------- #
# summary (§5.5 step 1)
# --------------------------------------------------------------------------- #

async def _final_report_text(slug: str, session_id: str) -> str:
    """The final report = the last assistant text in the child's checkpoint."""
    from langchain_core.messages import AIMessage

    from .checkpointer import FileCheckpointer
    from .graph import text_of_content

    try:
        tup = await FileCheckpointer(slug).aget_tuple(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:
        _log.exception("subagent_report_read_failed session=%s", session_id)
        return ""
    messages = ((tup.checkpoint.get("channel_values") or {}).get("messages") or []) if tup else []
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = text_of_content(getattr(m, "content", "")).strip()
            if text:
                return text
    return ""


async def _maybe_compress_summary(report: str, meta: dict) -> str:
    report = (report or "").strip()
    if not report:
        return "(subagent 未产生文本输出)"
    if len(report) <= SUMMARY_MAX_CHARS:
        return report
    try:
        if os.environ.get("GINNO_FAKE_LLM"):
            raise RuntimeError("GINNO_FAKE_LLM")  # deterministic-demo seam
        from langchain_core.messages import HumanMessage, SystemMessage

        from .models import build_model

        model = build_model(meta.get("provider"), meta.get("model"))
        resp = await model.ainvoke(
            [
                SystemMessage(
                    content=(
                        "把下面这份 subagent 最终报告压缩为不超过 500 字的摘要："
                        "保留结论、关键产出物路径、验收对照结果；"
                        "不要添加报告之外的信息，不要写开场白。"
                    )
                ),
                HumanMessage(content=report),
            ]
        )
        from .graph import text_of_content

        text = text_of_content(getattr(resp, "content", "")).strip()
        if text:
            return text
    except Exception:
        # Compression is best-effort; the truncated report is still truthful.
        _log.info(
            "subagent_summary_compress_skipped session=%s", meta.get("id")
        )
    return report[:SUMMARY_MAX_CHARS] + "…（原文过长，已截断）"


# --------------------------------------------------------------------------- #
# LLM title (P2 contract 6, design §5.9)
# --------------------------------------------------------------------------- #

# A verb phrase, deliberately shorter than the main-conversation subject title.
SUBAGENT_TITLE_MAX_CHARS = 16

_SUBAGENT_TITLE_SYSTEM = (
    "You are a session titler for a delegated subagent. Read its goal and the "
    "excerpt of its final report, then produce ONE very short verb-phrase "
    "title (at most 16 characters) naming WHAT it set out to do — e.g. "
    "「调研 OAuth 库选型」「编写集成测试骨架」. Not a sentence, no quotes, "
    "no trailing punctuation, no preamble. Answer in the goal's language."
)


async def _gen_subagent_title(child_id: str, report: str) -> None:
    """Generate the short title after the subagent's first completed turn.

    Degradation contract: GINNO_FAKE_LLM skips (deterministic demo), a model
    build/call failure or an unusable answer keeps the goal[:40] placeholder
    (the P1 behavior). Applied at most once — the ``subagent.titled`` flag on
    the meta is the claim, re-read with no await before the write (the same
    check-at-write discipline title_gen uses against a manual rename).
    """
    import os

    if os.environ.get("GINNO_FAKE_LLM"):
        return
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from .models import build_model
        from .title_gen import _content_text, _sanitize_title

        found = _find_meta(child_id)
        if not found or ((found[0].get("subagent") or {}).get("titled")):
            return
        meta, slug2 = found
        sa = meta.get("subagent") or {}
        goal = str(sa.get("goal") or "").strip()
        if not goal:
            return
        model = build_model(meta.get("provider"), meta.get("model"))
        resp = await model.ainvoke(
            [
                SystemMessage(content=_SUBAGENT_TITLE_SYSTEM),
                HumanMessage(
                    content=(
                        f"目标：{goal}\n"
                        f"最终报告节选：{(report or '').strip()[:800]}"
                    )
                ),
            ]
        )
        title = _sanitize_title(_content_text(resp))[:SUBAGENT_TITLE_MAX_CHARS].strip()
        if not title:
            _log.info("subagent_title_skipped session=%s reason=empty", child_id)
            return
        # Claim at write: only the first generator wins.
        found = _find_meta(child_id)
        if not found or ((found[0].get("subagent") or {}).get("titled")):
            return
        sa = dict(found[0].get("subagent") or {})
        sa["titled"] = True
        updated = _session_meta_patch(slug2, child_id, {"title": title, "subagent": sa})
        if updated is None:
            return  # deleted mid-flight
        _touch_in_memory(child_id, {"subagent": sa, "title": title})
        await _push_session_event(child_id, "session_title", {"title": title})
        _log.info(
            "subagent_titled session=%s title=%r", child_id, title,
        )
    except Exception:
        # Best-effort: the goal[:40] placeholder stays truthful.
        _log.info("subagent_title_failed session=%s", child_id)


# --------------------------------------------------------------------------- #
# stop cascade + listing + wait (contract 4/7)
# --------------------------------------------------------------------------- #

def _tree_ids(session_id: str, include_self: bool = True) -> list[str]:
    slug = _session_slug(session_id)
    ids = [session_id] if include_self else []
    if slug:
        ids += [m["id"] for m in _session_meta_descendants(slug, session_id)]
    return ids


async def _stop_parked_descendant(session_id: str, turn_id: str) -> None:
    """Heal + finalize one PARKED descendant (permission/ask_user interrupt) of
    a cascade stop. The same path the WS stop branch uses for a parked turn:
    heal the dangling checkpoint state, broadcast turn.stopped, drop the stash
    (result injections in it are reconciled first) and settle the subagent as
    "stopped" — otherwise the meta stays running forever, the sidebar shows a
    live dot and the checkpoint keeps an unresolved interrupt (major-4)."""
    from .api.sessions import _ensure_session  # lazy: cycle
    from .api.stream import _stop_parked_turn  # lazy: cycle

    session = _SESSIONS.get(session_id) or _ensure_session(session_id)
    if session is None:
        return
    await _stop_parked_turn(session, session_id, turn_id)


async def stop_subagent_tree(session_id: str, include_self: bool = True) -> list[str]:
    """Cooperatively stop the session + every descendant (contract 7, §5.6/5.7).

    Three descendant shapes, each settled explicitly so no meta is ever left
    running/waiting under a stopped ancestor:

    * live turn → set the cooperative event; the turn's own machinery
      finalizes "stopped" (the pre-arm discipline guarantees every live turn
      has an event);
    * parked at a permission/ask_user interrupt → no live task will ever
      observe an event: heal + finalize here (major-4);
    * waiting (or a stale running meta with no turn at all) → finalize
      "stopped" directly — a waiting subagent has no turn to signal, and
      leaving it would strand it ⏳/🟢 forever (major-3).

    Returns the ids a stop was signalled for (event set or parked-healed)."""
    stopped: list[str] = []
    for sid in _tree_ids(session_id, include_self):
        found = _find_meta(sid)
        live_sub = bool(
            found
            and _is_subagent(found[0])
            and _subagent_status(found[0]) not in TERMINAL_STATUSES
        )
        evt = _TURN_STOP.get(sid)
        armed_fresh = False
        if evt is not None:
            evt.set()
            stopped.append(sid)
        elif live_sub:
            # No event yet: every LIVE turn pre-arms one, so this is a QUEUED
            # wake (or one inside its flip startup) that holds or will take the
            # session's turn lock — arm the event anyway so the wake's
            # under-lock check refuses instead of opening a fresh turn on a
            # session being stopped.
            _TURN_STOP.setdefault(sid, asyncio.Event()).set()
            armed_fresh = True
        if sid in _PENDING_RESUME:
            _PENDING_RESUME.discard(sid)
            _PENDING_KIND.pop(sid, None)
            stopped.append(sid)
            # The parked turn keeps a _RUNNING_TURNS entry (its resume never
            # came) — pop it here, or the cascade-delete busy wait spins on it.
            tid = _RUNNING_TURNS.pop(sid, "")
            try:
                await _stop_parked_descendant(sid, tid)
            except Exception:
                _log.exception("subagent_stop_parked_descendant_failed session=%s", sid)
            continue
        if armed_fresh and sid not in _RUNNING_TURNS:
            task = _TURN_TASKS.get(sid)
            if task is None or task.done():
                # Nothing live will ever observe the event we just armed (a
                # waiting session, or the queued-wake case above): settle it
                # stopped here, and drop the event so it cannot kill a future
                # legitimate turn. A session with a live/starting turn is left
                # to its own machinery via the event.
                _TURN_STOP.pop(sid, None)
                try:
                    await _finalize_subagent(found[1], found[0], "stopped")
                except Exception:
                    _log.exception("subagent_stop_finalize_failed session=%s", sid)
    return stopped


async def stop_descendants(session_id: str) -> list[str]:
    """Cascade half of stop_subagent_tree (the session's own turn is handled by
    the WS stop branch that calls this)."""
    return await stop_subagent_tree(session_id, include_self=False)


def collect_subagent_rows(
    slug: str, session_id: str, include_descendants: bool = True
) -> list[dict]:
    """[{session_id, title, goal, status, depth, parent_session_id, elapsed_s}]
    for the direct children (and, optionally, all further descendants)."""
    rows: list[dict] = []

    def _row(m: dict) -> dict:
        sa = m.get("subagent") or {}
        return {
            "session_id": m["id"],
            "title": m.get("title") or "",
            "goal": str(sa.get("goal") or ""),
            "status": str(sa.get("status") or ""),
            "depth": int(m.get("depth") or 0),
            "parent_session_id": m.get("parent_session_id"),
            "elapsed_s": round(max(0.0, time.time() - float(m.get("created") or time.time())), 1),
        }

    from .session_meta import _session_meta_children

    def _walk(pid: str) -> None:
        for m in _session_meta_children(slug, pid):
            rows.append(_row(m))
            if include_descendants:
                _walk(m["id"])

    _walk(session_id)
    return rows


async def wait_for_subagents(
    parent_session_id: str, ids: list[str] | None = None, timeout_s: int = 0
) -> str:
    """Block until the named (default: all DIRECT) children reach a terminal
    status; returns their status summaries as JSON. ``timeout_s > 0`` returns a
    marked partial at the deadline. Interruptible by the session's cooperative
    stop signal — the tool returns a ``[stopped]`` marker and the turn ends at
    the next chunk boundary, exactly like any other stopped turn."""
    slug = _session_slug(parent_session_id) or "default"
    deadline = time.monotonic() + max(0, int(timeout_s or 0)) if (timeout_s or 0) > 0 else None

    def _snapshot() -> tuple[list[dict], list[str]]:
        rows = collect_subagent_rows(slug, parent_session_id, include_descendants=True)
        by_id = {r["session_id"]: r for r in rows}
        if ids:
            chosen = [by_id[i] for i in ids if i in by_id]
            missing = [i for i in ids if i not in by_id]
        else:
            chosen = [r for r in rows if r.get("parent_session_id") == parent_session_id]
            missing = []
        return chosen, missing

    chosen, missing = _snapshot()
    if ids and missing and not chosen:
        # Nothing resolvable: waiting forever would be a deadlock-shaped bug —
        # answer with the error instead.
        return f"[error] 未找到指定的 subagent：{', '.join(missing)}"

    while True:
        chosen, missing = _snapshot()
        if not missing and (
            not chosen or all(r["status"] in TERMINAL_STATUSES for r in chosen)
        ):
            return json.dumps(
                {"partial": False, "subagents": _wait_rows(chosen)}, ensure_ascii=False
            )
        evt = _TURN_STOP.get(parent_session_id)
        if evt is not None and evt.is_set():
            return "[stopped] 用户停止了当前回合，等待中断。当前子代状态：\n" + json.dumps(
                {"partial": True, "subagents": _wait_rows(chosen)}, ensure_ascii=False
            )
        if deadline is not None and time.monotonic() >= deadline:
            return "(partial) 等待超时，子代尚未全部结束：\n" + json.dumps(
                {"partial": True, "subagents": _wait_rows(chosen)}, ensure_ascii=False
            )
        await asyncio.sleep(0.2)


def _wait_rows(rows: list[dict]) -> list[dict]:
    """Status summaries for the wait result — result_summary rides along once
    the child finished (that is the whole point of waiting)."""
    out: list[dict] = []
    for r in rows:
        item = {k: r[k] for k in ("session_id", "title", "goal", "status", "elapsed_s")}
        if r["status"] == "done":
            found = _find_meta(r["session_id"])
            if found:
                item["result_summary"] = str(
                    (found[0].get("subagent") or {}).get("result_summary") or ""
                )
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# cascade delete (§5.8)
# --------------------------------------------------------------------------- #

async def finalize_for_delete(session_id: str) -> None:
    """Delete-path settlement (§5.8, major-5): the DELETE endpoint removes the
    meta directly, so the settle hook can never find it again (_find_meta →
    None → return) — a waiting ancestor of the removed session would stay
    ⏳ forever with no one left to wake it. Finalize a live subagent as
    "stopped" BEFORE its meta disappears, then re-evaluate every surviving
    waiting ancestor up the chain."""
    found = _find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if _is_subagent(meta) and _subagent_status(meta) not in TERMINAL_STATUSES:
        # Stop a live turn so it unwinds instead of writing into a deleted
        # checkpoint; its settle hook no-ops (meta gone / already terminal).
        evt = _TURN_STOP.get(session_id)
        if evt is not None:
            evt.set()
        try:
            await _finalize_subagent(slug, meta, "stopped")
        except Exception:
            _log.exception("subagent_delete_finalize_failed session=%s", session_id)
    seen: set[str] = {session_id}
    pid = meta.get("parent_session_id")
    while pid and pid not in seen:
        seen.add(pid)
        try:
            await _reevaluate_waiting_parent(pid)
        except Exception:
            _log.exception("subagent_delete_reevaluate_failed parent=%s", pid)
        pfound = _find_meta(pid)
        pid = pfound[0].get("parent_session_id") if pfound else None


async def delete_subagent_tree(slug: str, session_id: str) -> list[str]:
    """Stop + remove every DESCENDANT of ``session_id`` (the caller removes the
    session itself). Order matters: cooperative stop → wait for the turn tasks
    to unwind → only then delete checkpoint files, so nothing writes back into
    a deleted file."""
    ids = [m["id"] for m in _session_meta_descendants(slug, session_id)]
    if not ids and session_id not in _SESSIONS:
        return []
    await stop_subagent_tree(session_id, include_self=True)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        # A PARKED session (permission/ask_user interrupt) keeps a permanent
        # _RUNNING_TURNS entry with no live task behind it — the cascade stop
        # above already healed + popped it, but exclude pending-resume ids
        # explicitly so a straggler can never spin the wait to the 10s cap
        # (major-4).
        busy = [
            sid
            for sid in ([session_id] + ids)
            if sid not in _PENDING_RESUME
            and (
                sid in _RUNNING_TURNS
                or ((_TURN_TASKS.get(sid) is not None) and not _TURN_TASKS[sid].done())
            )
        ]
        if not busy:
            break
        await asyncio.sleep(0.1)
    for cid in ids:
        _SESSIONS.pop(cid, None)
        _PENDING_RESUME.discard(cid)
        _PENDING_KIND.pop(cid, None)
        _TURN_STOP.pop(cid, None)
        from .server_shared import subagent_unlink_any

        subagent_unlink_any(cid)
        _session_meta_remove(slug, cid)
        cp = paths.project_sessions_dir(slug) / f"{cid}.json"
        if cp.exists():
            try:
                cp.unlink()
            except OSError:
                _log.warning("subagent_cascade_unlink_failed session=%s", cid)
        _log.info("subagent_cascade_deleted parent=%s child=%s", session_id, cid)
    return ids
