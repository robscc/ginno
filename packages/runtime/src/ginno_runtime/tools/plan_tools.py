"""Agent-facing plan tools（subagent-plan-orchestration-design.md §5）.

主 agent 的编排入口：建清单 / 改单 / 取消。调度由 watcher 负责（工具
不 spawn）；条目终态经去抖合流注入回流本会话（[plan update] 信封）。

与 spawn_subagent 的分工：单件小任务直发 spawn_subagent（零开销路径）；
可切分、有依赖、要进度的批量工作用清单。
"""

from __future__ import annotations

from langchain_core.tools import tool

from .. import paths
from ..plans import store, watcher


def build_plan_tools(
    session_id: str | None, project_slug: str | None
) -> list:
    """绑定会话的清单工具；无 session（workflow/列举端点）→ 空。"""
    if not session_id:
        return []
    slug = project_slug or "default"

    @tool
    def create_subagent_plan(
        title: str,
        items: list[dict],
        needs_confirm: bool = False,
    ) -> str:
        """Create a task-list plan; the system watches it, launches one
        subagent (or delegation run) per item as its dependencies finish,
        and reports status changes back to you automatically. Use this
        INSTEAD of calling spawn_subagent many times: one plan call covers
        the whole batch (dependencies, concurrency, progress, retries via
        amend).

        Finish the turn right after creating the plan — statuses will be
        injected into this conversation as items settle; then verify results,
        amend failed items, and deliver the final report.

        Args:
            title: Short plan title.
            items: List of {goal (required, self-contained), constraints,
                acceptance, agent_type (subagent type e.g. implementer/
                researcher; omit = default), backend ("ginno" = default
                subagent; or a delegation backend name — "claude-code",
                "codex", "pi" — choose per item whichever fits the task:
                heavyweight coding/CLI work suits a delegation backend,
                in-conversation tool work suits ginno), mode ("read-only"
                (default) or "edit" — delegation only), depends_on (list of
                earlier item_ids that must be DONE first)}. Item ids default
                to it_1, it_2, … in order.
            needs_confirm: True = hold for user confirmation before
                scheduling (use for risky/destructive or credit-spending
                batches). Default False = start immediately.
        """
        try:
            plan = store.create_plan(
                session_id,
                slug=slug,
                title=title,
                items=items,
                needs_confirm=bool(needs_confirm),
                author="agent",
            )
        except ValueError as e:
            return f"[error] invalid plan: {e}"
        watcher.notify_change(session_id)
        n = len(plan["items"])
        state = (
            "awaiting user confirmation"
            if plan["status"] == store.PLAN_PENDING_CONFIRM
            else "scheduling started"
        )
        return (
            f"[plan created] {plan['plan_id']} \"{plan['title']}\" — {n} item(s), {state}.\n"
            f"Item ids: {', '.join(it['item_id'] for it in plan['items'])}.\n"
            "Status changes will be injected here automatically as [plan update]; "
            "respond to them by verifying results, amend_plan(reset_items=[...]) "
            "to retry failures, and report when the plan completes."
        )

    @tool
    def amend_plan(
        plan_id: str,
        add_items: list[dict] | None = None,
        remove_items: list[str] | None = None,
        reset_items: list[str] | None = None,
        update_items: dict[str, dict] | None = None,
    ) -> str:
        """Amend an existing plan: add / remove / retry / edit items.

        Args:
            plan_id: The plan id from create_subagent_plan.
            add_items: New items, same shape as create (depends_on may
                reference existing item ids).
            remove_items: Item ids to drop (not while running).
            reset_items: Item ids to reset to pending = RETRY (keeps a
                attempts counter; edit goal/update_items first if the
                previous attempt's brief was the problem).
            update_items: {item_id: {goal|constraints|acceptance|
                agent_type|backend|mode}} — only items not yet started.
        """
        try:
            plan, err = store.amend_plan(
                slug,
                session_id,
                plan_id,
                add=add_items,
                remove=remove_items,
                reset=reset_items,
                update=update_items,
            )
        except ValueError as e:
            return f"[error] {e}"
        if plan is None:
            return f"[error] {err}"
        watcher.notify_change(session_id)
        counts = {}
        for it in plan["items"]:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
        stat = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        return f"[plan amended] {plan_id} v{plan['version']} — {stat}."

    @tool
    def cancel_plan(plan_id: str) -> str:
        """Cancel a plan: unstarted items are dropped; running subagent
        children are stopped (delegation runs finish under their own
        timeout). Use when the plan's goal became moot."""
        plan = store.cancel_plan(slug, session_id, plan_id)
        if plan is None:
            return f"[error] plan not found or already terminal: {plan_id}"
        watcher.notify_change(session_id)
        return f"[plan cancelled] {plan_id}."

    return [create_subagent_plan, amend_plan, cancel_plan]
