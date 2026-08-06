"""Goal tools — let the agent read/create/update the session's long-running goal.

Availability per agent is gated by tools_allow patterns (see
registry.ensure_goal_tools): dev keeps them via ``*``; research/writer get an
explicit ``goal_*`` pattern.

Design notes (goal-design.md §4.2):
* The model may only mark a goal ``complete`` or ``blocked`` — pause/resume are
  user-controlled (Codex parity).
* ``goal_create`` fails when an unfinished goal exists.
* The "3 consecutive blocked turns" audit is a PROMPT-level rule (review
  decision A) — there is no server-side counter.
* Tools are bound to (project_slug, session_id) at construction because the
  goal store is keyed per session.
"""

from __future__ import annotations

from langchain_core.tools import tool as _tool

from ..goals import store as goal_store
from ..goals.events import notify_goal_changed

GOAL_TOOL_NAMES = {"goal_get", "goal_create", "goal_update"}


def build_goal_tools(project_slug: str, session_id: str):
    """Return the three goal tools bound to this session's store coordinates."""
    slug, sid = project_slug, session_id

    def goal_get() -> str:
        """Get the current goal for this session: objective, status, elapsed
        time and goal-turn count. Returns '(no goal)' when none is set."""
        g = goal_store.get_goal(slug, sid)
        if not g:
            return "(no goal)"
        mins = int(g.get("time_used_seconds", 0)) // 60
        return (
            f"objective: {g['objective']}\n"
            f"status: {g['status']}\n"
            f"goal_turns: {g.get('turns_used', 0)}\n"
            f"time_used: {mins}m"
        )

    def goal_create(objective: str) -> str:
        """Create a goal ONLY when the user (or system instructions) explicitly
        asks for a long-running objective; do not infer goals from ordinary
        tasks. Fails if this session already has an unfinished goal — complete
        or clear it first. `objective` is the full long-running objective."""
        try:
            g = goal_store.create_goal(slug, sid, objective)
        except goal_store.GoalConflictError as e:
            return f"cannot create goal: {e}"
        except ValueError as e:
            return f"invalid goal: {e}"
        notify_goal_changed(slug, sid, g)
        return f"created goal ({g['status']}): {g['objective']}"

    def goal_update(status: str) -> str:
        """Update the existing goal's status. `status` must be exactly one of:
        'complete' | 'blocked'. You cannot pause, resume or clear a goal — those
        are user actions.

        Set status to 'complete' ONLY when the objective has actually been
        achieved and no required work remains. Before completing, verify against
        the actual current state (completion audit). When completing, briefly
        summarize what was achieved.

        Set status to 'blocked' ONLY when the same blocking condition has
        repeated for at least three consecutive goal turns and you cannot make
        meaningful progress without user input or an external change. Do NOT
        mark blocked just because the work is hard, slow, or incomplete. If you
        resume a previously blocked goal, treat it as a fresh blocked audit."""
        s = (status or "").strip().lower()
        if s not in ("complete", "blocked"):
            return (
                "goal_update only accepts 'complete' or 'blocked'; pause/resume/"
                "clear are controlled by the user."
            )
        g = goal_store.update_status(slug, sid, s)
        if not g:
            return "no goal to update"
        notify_goal_changed(slug, sid, g)
        return f"goal status set to '{s}'"

    # Decorate as LangChain tools (names derived from the function names).
    return [
        _tool(goal_get),
        _tool(goal_create),
        _tool(goal_update),
    ]
