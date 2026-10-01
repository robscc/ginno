"""``/subagent 拆分`` — LLM task decomposition + the user confirmation plan.

P2 contract (docs/subagent-design.md §5.1/§5.2, appendix A.2/A.7/A.8):

* the split form of ``/subagent`` runs ONE decompose call with the session's
  own model (prompt = the A.2 "implementation" template + A.8 output control:
  assistant prefill ``[`` and "no preamble") and STRICTLY validates the JSON
  array — a non-array, an unparsable body, a missing ``goal``/``reason``, or
  more subtasks than the spawn concurrency cap all fail the WHOLE plan (no
  half-built plan is ever shown);
* the validated plan is stored per session (a new split overwrites the old
  unconfirmed one), broadcast as a ``subagent.plan`` WS event to the session's
  sockets, and waits for the user's card action;
* ``subagent.plan.confirm`` spawns every subtask through the EXISTING spawn
  path (``subagent_scheduler.create_subagent``, ``origin="user"``) and answers
  with one summary notice; ``subagent.plan.cancel`` just drops the plan.

Import discipline mirrors the scheduler: light top-level imports only; the
model factory and the scheduler's spawn path are imported lazily.
"""

from __future__ import annotations

import json
import time
import uuid

from .server_shared import _log, _push_session_event
from .subagent_scheduler import max_concurrent

# P3 contract 5 (P2 遗留): these builtin-async commands run OFF the WS receive
# loop (spawn_bg) — the decompose LLM call takes tens of seconds and must not
# block the socket's ping/stop/steer. The invoking socket still gets an
# immediate message.end; the result notice + the subagent.plan broadcast land
# when the job finishes.
BACKGROUND_ASYNC_COMMANDS = frozenset({"subagent", "subagent-split"})

# Per-session pending plan: {session_id: {plan_id, session_id, task, subtasks,
# created}}. Keyed by SESSION (not plan_id) so a second ``/subagent 拆分``
# before confirmation simply overwrites the old plan (P2 contract 3);
# confirm/cancel address it with the shown plan_id.
_PENDING_PLANS: dict[str, dict] = {}

# Decompose prompt — appendix A.2 (implementation template) as the skeleton,
# with A.7's per-subtask 委派理由 (``reason``) added and A.8's output control
# as the closing line. The assistant prefill "[" lives in ``decompose_task``.
_DECOMPOSE_SYSTEM = """你是任务拆解器。输入一个工程任务，输出并行子任务方案。

流程：
1. 关键分析（只做一次）：用多级列表拆解任务的关键方面与风险
2. 子任务划分：每个子任务必须是——
   - 可独立完成、可单独验收（有明确的完成判据）
   - 相互之间文件/资源不冲突（并发执行安全），冲突的必须串成依赖说明
   - 粒度适中：单个子任务预期 5-30 分钟工作量；更小的合并、更大的再拆
3. 为每个子任务写：goal（自足的完整描述——子代理看不到本对话，goal 必须能独立执行）、
   constraints（不可碰的边界）、acceptance（验收标准）、reason（委派理由：
   为什么适合独立委派，例如「纯只读调研，独立上下文即可完成」；
   若理由是「需要本对话的细节」，说明该任务不该委派，不要列入）

输出 JSON 数组，不要任何前言或解释。"""


def _extract_json_array(raw: str) -> list | None:
    """Model output -> a JSON array, or None. Tolerates markdown code fences
    and a stray leading "[" (the prefill echo); STRICT about the structure
    itself — anything that is not one JSON array returns None."""
    t = (raw or "").strip()
    if not t:
        return None
    if t.startswith("```"):  # one wrapping code fence, despite instructions
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:].lstrip()
    if not t.startswith("["):
        t = "[" + t  # the assistant prefill "[" — the model continues after it
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def validate_subtasks(raw) -> tuple[list[dict] | None, str]:
    """Strict validation of the decompose output (P2 contract 3): every item
    needs a non-empty string ``goal`` AND a non-empty string ``reason``
    (appendix A.7 — the 委派理由 is the main thing the user reviews on the
    confirmation card). ``constraints``/``acceptance`` are optional strings.
    Any failure rejects the WHOLE plan."""
    if not isinstance(raw, list):
        return None, "模型输出不是 JSON 数组"
    if not raw:
        return None, "模型没有拆出任何子任务"
    cap = max_concurrent()
    if len(raw) > cap:
        return None, (
            f"拆出了 {len(raw)} 个子任务，超过并发上限（{cap}）；"
            "请把任务描述得更聚焦后重试"
        )
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"第 {i + 1} 个子任务不是对象"
        goal = item.get("goal")
        reason = item.get("reason")
        if not isinstance(goal, str) or not goal.strip():
            return None, f"第 {i + 1} 个子任务缺少 goal"
        if not isinstance(reason, str) or not reason.strip():
            return None, f"第 {i + 1} 个子任务缺少 reason（委派理由）"
        out.append(
            {
                "goal": goal.strip(),
                "constraints": str(item.get("constraints") or "").strip(),
                "acceptance": str(item.get("acceptance") or "").strip(),
                "reason": reason.strip(),
            }
        )
    return out, ""


async def decompose_task(session: dict, task: str) -> list[dict]:
    """One decompose LLM call with the session's own model. Raises ValueError
    (user-facing message) on any invalid output."""
    import os

    if os.environ.get("GINNO_FAKE_LLM"):
        # The scripted demo model cannot produce a real plan; fail loudly
        # instead of showing a fake one.
        raise ValueError("GINNO_FAKE_LLM 演示模式下不可用，请配置真实模型后重试")
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from .models import build_model
    from .graph import text_of_content

    model = build_model(session.get("model_provider"), session.get("model_name"))
    resp = await model.ainvoke(
        [
            SystemMessage(content=_DECOMPOSE_SYSTEM),
            HumanMessage(content=(task or "").strip()),
            AIMessage(content="["),  # A.8: prefill the opening bracket
        ]
    )
    raw = text_of_content(getattr(resp, "content", ""))
    data = _extract_json_array(raw)
    if data is None:
        _log.info("subagent_decompose_invalid session=%s raw=%r", session.get("session_id"), raw[:200])
        raise ValueError("模型输出无法解析为子任务 JSON 数组，请重试")
    subtasks, err = validate_subtasks(data)
    if subtasks is None:
        raise ValueError(err)
    return subtasks


async def issue_plan(session: dict, task: str, subtasks: list[dict]) -> str:
    """Store + broadcast one plan; returns the notice text for the command."""
    session_id = session["session_id"]
    plan = {
        "plan_id": uuid.uuid4().hex[:12],
        "session_id": session_id,
        "task": (task or "").strip(),
        "subtasks": subtasks,
        "created": time.time(),
    }
    # Overwrite any previous unconfirmed plan for this session (P2 contract 3).
    _PENDING_PLANS[session_id] = plan
    await _push_session_event(
        session_id,
        "subagent.plan",
        {
            "plan_id": plan["plan_id"],
            "session_id": session_id,
            "task": plan["task"],
            "subtasks": subtasks,
        },
    )
    _log.info(
        "subagent_plan_issued session=%s plan=%s subtasks=%d",
        session_id, plan["plan_id"], len(subtasks),
    )
    lines = [f"已生成拆分方案（{len(subtasks)} 个子任务），请在确认卡片中查看："]
    lines += [
        f"{i}. {st['goal']} —— 委派理由：{st['reason']}"
        for i, st in enumerate(subtasks, 1)
    ]
    lines.append("确认后逐个启动；未确认前再次 /subagent 拆分 会覆盖本方案。")
    return "\n".join(lines)


async def decompose_and_issue(session: dict, task: str) -> str:
    """decompose → validate → issue; the ``/subagent 拆分`` handler body."""
    subtasks = await decompose_task(session, task)
    return await issue_plan(session, task, subtasks)


def get_pending_plan(session_id: str, plan_id: str | None = None) -> dict | None:
    """The session's pending plan, optionally checked against ``plan_id``."""
    plan = _PENDING_PLANS.get(session_id)
    if plan is None:
        return None
    if plan_id is not None and plan["plan_id"] != plan_id:
        return None
    return plan


def cancel_plan(session_id: str, plan_id: str) -> bool:
    """Drop the pending plan (``subagent.plan.cancel``). Idempotent."""
    plan = _PENDING_PLANS.get(session_id)
    if plan is None or plan["plan_id"] != plan_id:
        return False
    del _PENDING_PLANS[session_id]
    _log.info("subagent_plan_cancelled session=%s plan=%s", session_id, plan_id)
    return True


async def cancel_plan_and_broadcast(session_id: str, plan_id: str) -> bool:
    """cancel + broadcast ``subagent.plan.cancelled { plan_id }`` (P3 contract
    5): every tab holding the pending card flips it to 已取消. The broadcast
    fires even when the store no-ops (already confirmed / overwritten /
    unknown id) — the event is UI-state reconciliation, and a tab that never
    saw the confirm race still gets out of the pending state."""
    try:
        cancelled = cancel_plan(session_id, plan_id)
    except Exception:
        _log.exception("subagent_plan_cancel_failed session=%s", session_id)
        cancelled = False
    try:
        await _push_session_event(
            session_id,
            "subagent.plan.cancelled",
            {"plan_id": plan_id, "session_id": session_id},
        )
    except Exception:
        pass
    return cancelled


async def run_background_command(
    session: dict, name: str, args: str, turn_id: str = ""
) -> None:
    """The WS background job for the subagent command family (P3 contract 5,
    P2 遗留): run the builtin async handler OFF the receive loop and broadcast
    its reply as a ``notice``. The invoking socket gets its message.end
    immediately (the WS branch), so ping/stop/steer keep flowing while the
    decompose LLM call runs; the split form's ``subagent.plan`` broadcast
    happens inside issue_plan on the same completion path (契约不变)."""
    from .commands.registry import BUILTINS

    try:
        reply = await BUILTINS[name].async_handler(
            session.get("project_slug"), session, args
        )
    except Exception as e:
        _log.exception(
            "subagent_async_job_failed name=%s session=%s",
            name, session.get("session_id"),
        )
        reply = f"/{name} 执行失败：{type(e).__name__}: {e}"
    try:
        await _push_session_event(
            session.get("session_id") or "", "notice", {"message": reply}, turn_id
        )
    except Exception:
        pass


async def confirm_plan(
    session_id: str, plan_id: str, subtasks_override=None
) -> str:
    """Spawn every subtask of the confirmed plan (P2 contract 2): the EDITED
    subtasks from the card win when provided; each goes through the EXISTING
    spawn path with ``origin="user"``. Returns the summary notice text."""
    plan = get_pending_plan(session_id, plan_id)
    if plan is None:
        # The refusal MUST be pushed, not just returned: the WS handler drops
        # the return value, and the confirming tab has already flipped its card
        # to 「已确认」 optimistically — without the notice, a confirm that
        # raced a cancel/overwrite (second tab, re-split) loses the whole batch
        # silently.
        text = "确认未生效：未找到待确认的拆分方案（可能已在其他窗口确认/取消，或被新方案覆盖）"
        await _push_session_event(session_id, "notice", {"message": text})
        return text
    subtasks = plan["subtasks"]
    if subtasks_override:
        subtasks, err = validate_subtasks(subtasks_override)
        if subtasks is None:
            text = f"编辑后的子任务无效：{err}"
            await _push_session_event(session_id, "notice", {"message": text})
            return text
    del _PENDING_PLANS[session_id]

    from .subagent_scheduler import create_subagent  # lazy: heavy import chain

    spawned: list[str] = []
    failed: list[str] = []
    for st in subtasks:
        res = await create_subagent(
            session_id,
            st["goal"],
            st["constraints"],
            st["acceptance"],
            origin="user",
        )
        if res.get("ok"):
            spawned.append(f"{res['session_id']} {st['goal'][:40]}")
        else:
            failed.append(f"{st['goal'][:40]}：{res.get('error') or '未知错误'}")
    lines = [f"拆分方案已确认：成功启动 {len(spawned)} 个 subagent。"]
    lines += [f"- ✅ {s}" for s in spawned]
    if failed:
        lines.append(f"{len(failed)} 个启动失败：")
        lines += [f"- ❌ {f}" for f in failed]
    text = "\n".join(lines)
    await _push_session_event(session_id, "notice", {"message": text})
    _log.info(
        "subagent_plan_confirmed session=%s plan=%s ok=%d failed=%d",
        session_id, plan_id, len(spawned), len(failed),
    )
    return text
