"""ask_user — the ambiguity-resolution tool.

Every other tool in the set either acts or fails; none of them can *ask*. The
2026-09-17 incident is what that costs: the user said "导入 skill" while the
session had been writing into a repo at ``~/workspace/dev/claude-agent-team``
that carried its own ``.claude/skills``. "Import" was ambiguous (Ginno's
``~/.ginno/skills`` vs. that repo's ``.claude/skills``), the model could only
guess, it guessed Ginno, and the user had to correct it in prose 82 model
calls later.

This tool parks the turn on a LangGraph ``interrupt`` (the same machinery the
permission node and ``workflow_propose_edit`` use — see
``tools/workflow_tools.py`` for the in-tool precedent) so the user can pick.
The server emits a ``user.question`` event and resumes with
``{"kind": "user_answer", ...}``.
"""

from __future__ import annotations

import contextvars
import json
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt

ASK_TOOL_NAMES = {"ask_user"}

# Autonomous turns (the goal continuation driver) have nobody to answer. A
# parked turn there would hang until the stop signal, so the tool refuses and
# tells the model to decide and say what it assumed. Same shape as the
# web-tools' opt-in reasoning: a capability that needs a human must not fire
# where no human is watching.
_INTERACTIVE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "ginno_interactive_turn", default=True
)

# Per-turn ask budget, armed by api/stream.py at the start of each turn.
_BUDGET: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ginno_ask_budget", default=None
)


def set_interactive(flag: bool) -> contextvars.Token:
    return _INTERACTIVE.set(bool(flag))


def reset_interactive(token: contextvars.Token) -> None:
    _INTERACTIVE.reset(token)


def begin_ask_budget() -> contextvars.Token:
    """Arm a fresh per-turn budget. Returns a token for the turn's finally."""
    return _BUDGET.set({"n": 0})


def reset_ask_budget(token: contextvars.Token) -> None:
    _BUDGET.reset(token)


def normalize_options(raw: object) -> list[str]:
    """Coerce an ``options`` argument into a list of labels.

    Models routinely JSON-ENCODE an array argument instead of sending a real
    array — observed live (2026-09-20, turn aa49c475): ``options`` arrived as
    the string ``'["装进仓库", "装进全局"]'``. Rejecting that via schema
    validation burned the model's whole ask budget on two retries before it
    gave up and asked as free text; and anything that iterated the string
    rendered one card option PER CHARACTER.

    So: a list is taken as-is; a string is parsed as JSON when it looks like a
    list, taken as ONE bare option otherwise; anything else is empty.
    """
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("["):
            # An encoded array. A malformed one yields NOTHING rather than
            # per-character garbage.
            try:
                parsed = json.loads(s)
            except json.JSONDecodeError:
                return []
            raw = parsed if isinstance(parsed, list) else []
        else:
            # Anything else is one bare label (a single-option question).
            raw = [s]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for o in raw:
        label = str(o).strip()
        if label:
            out.append(label)
    return out


def build_ask_tools(project_slug: str | None = None, session_id: str | None = None) -> list:
    """Built per session, like the skill tools.

    Without a ``session_id`` there is no session socket to answer over (the
    workflow engine and the listing endpoints call ``build_all_tools`` with
    none), so the tool is simply absent — a workflow already has its
    first-class ``human`` node for this.
    """
    if not session_id:
        return []

    @tool
    def ask_user(
        question: str,
        options: list[str] | str | None = None,
        header: str = "",
        allow_free_text: bool = True,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Ask the user to choose, when their answer changes what you do next.

        Use this ONLY when a reasonable user could mean two different things
        and guessing wrong wastes real work — e.g. "导入 skill" (install into
        Ginno's ~/.ginno/skills, into this project's skills dir, or into the
        repo's .claude/skills?). Do NOT use it to confirm an obvious step, to
        ask for permission (the permission system handles that), or to ask for
        information you can find yourself with the file/shell tools.

        Call this as the ONLY tool call in its step. If you call it alongside
        another tool, that tool runs twice when the turn resumes, so a
        side-effecting sibling would execute a second time.

        ``question``  — the body, markdown allowed. State the ambiguity
                        concretely, and name the candidates.
        ``options``   — 2-5 user-facing choices, as a REAL JSON ARRAY. Omit
                        entirely for a pure free-text question. Each entry must
                        be a complete, self-explanatory label ("安装到
                        <repo>/.claude/skills（Claude Code 读取）"), because that
                        exact string is what the user clicks and what you get
                        back. Do NOT pass a JSON-encoded string of an array
                        (it is accepted, but a real array is clearer).
                        Never write the choices as "1 / 2 / 3" inside
                        ``question`` — put them here so they become buttons.
        ``header``    — 2-4 word card title, e.g. "选择安装位置".
        ``allow_free_text`` — also offer an "其他" field (default true).

        Returns JSON: {ok, skipped, source, option_index, answer, free_text}
        where source is "option" | "free_text" | "skipped". When ``skipped``
        is true the user declined to choose — proceed with your best
        judgement and SAY WHICH ASSUMPTION YOU MADE in your reply, so it can
        be corrected.
        """
        q = (question or "").strip()
        if not q:
            return "[error] question is required"
        opts = normalize_options(options)

        if not _INTERACTIVE.get():
            return (
                "[error] 当前是自主续跑（无人值守）回合，没有人能回答。"
                "请选择最合理的默认继续，并在回复中明确写出你选了什么，以便用户纠正。"
            )

        from ..world_state import (  # local: avoid import cycle at module load
            DEFAULT_ASK_MAX_PER_TURN,
            context_settings,
        )

        try:
            max_asks = int(
                context_settings().get("ask_user_max_per_turn", DEFAULT_ASK_MAX_PER_TURN)
            )
        except (TypeError, ValueError):
            max_asks = DEFAULT_ASK_MAX_PER_TURN

        budget = _BUDGET.get()
        if budget is not None:
            if budget["n"] >= max_asks:
                return (
                    f"[error] 本轮已询问用户 {budget['n']} 次（上限 {max_asks}）。"
                    "请自行决定，并把你的假定写进回复。"
                )
            budget["n"] += 1

        answer = interrupt(
            {
                "kind": "user_question",
                # The tool_call id rides the interrupt so the re-emit path can
                # rebuild the same question from the checkpoint alone (the id
                # is stable in the checkpoint, the live event, and replay —
                # which is what makes the client's merge idempotent).
                "id": tool_call_id or "",
                "question": q,
                "options": opts,
                "header": (header or "").strip(),
                "allow_free_text": bool(allow_free_text),
            }
        )

        if not isinstance(answer, dict) or answer.get("skip"):
            return json.dumps(
                {"ok": True, "skipped": True, "source": "skipped",
                 "option_index": None, "answer": "", "free_text": ""},
                ensure_ascii=False,
            )

        text = str(answer.get("answer") or "").strip()
        idx = answer.get("option_index")
        if isinstance(idx, int) and 0 <= idx < len(opts):
            return json.dumps(
                {
                    "ok": True,
                    "skipped": False,
                    "source": "option",
                    "option_index": idx,
                    "answer": opts[idx],
                    # The user may have picked a chip AND typed something —
                    # keep the extra text rather than silently dropping it.
                    "free_text": text if text and text != opts[idx] else "",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "skipped": False, "source": "free_text",
             "option_index": None, "answer": text, "free_text": ""},
            ensure_ascii=False,
        )

    return [ask_user]