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

import ast
import contextvars
import re
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import interrupt

from ginno_runtime import jsonx as json

from ..lang import t

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


# 富选项对象里可能承载标签的键，按优先级。模型有时把 options 传成
# ``[{label, description, key}, …]`` 这类对象数组（2026-10-29 实测：卡片按钮
# 渲染出整段 dict 字面量，而不是 label）。
_LABEL_KEYS = ("label", "text", "title", "name", "value", "option", "description")
# JS 风格对象字面量（键不带引号）literal_eval/json 都解析不了时的兜底：
# 直接抽 ``label: '…'`` 这类键值。
_OBJ_LABEL_RE = re.compile(
    r"""(?<![\w'"])(?:label|text|title|name|option)['"]?\s*:\s*(['"])(.*?)\1""", re.S
)


def _dict_label(d: dict) -> str:
    for k in _LABEL_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _option_label(o: object) -> str:
    """One option entry → its display label.

    接受三种形状：普通字符串；富对象 ``{label, description}``（取 label）；
    以及把对象字面量整个当字符串传进来的条目（反序列化后取 label）。
    认不出 label 键时原样保留，绝不丢信息。
    """
    if isinstance(o, dict):
        return _dict_label(o) or str(o).strip()
    s = str(o).strip()
    if not s.startswith("{"):
        return s
    parsed: object = None
    try:
        parsed = ast.literal_eval(s)
    except (ValueError, SyntaxError, TypeError):
        try:
            parsed = json.loads(s)
        except Exception:
            parsed = None
    if isinstance(parsed, dict):
        label = _dict_label(parsed)
        if label:
            return label
        return s
    m = _OBJ_LABEL_RE.search(s)
    return m.group(2).strip() if m and m.group(2).strip() else s


def normalize_options(raw: object) -> list[str]:
    """Coerce an ``options`` argument into a list of labels.

    Models routinely JSON-ENCODE an array argument instead of sending a real
    array — observed live (2026-09-20, turn aa49c475): ``options`` arrived as
    the string ``'["装进仓库", "装进全局"]'``. Rejecting that via schema
    validation burned the model's whole ask budget on two retries before it
    gave up and asked as free text; and anything that iterated the string
    rendered one card option PER CHARACTER.

    Also unwraps rich option objects to their label — observed live
    (2026-10-29): ``options`` as ``[{description, key, label}, …]`` rendered
    the whole dict literal on the card's buttons.

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
        label = _option_label(o)
        if label:
            out.append(label)
    return out


def _multi_receipt(subs: list[dict], answer: object) -> str:
    """多问题回执：逐题 {question, source, option_index, answer}。整卡跳过
    （stop/非 dict 回复）时每题记 skipped——模型据此自行假定并声明。"""
    results: list[dict] = []

    def _label(s: dict) -> str:
        return s["header"] or s["question"]

    if not isinstance(answer, dict) or answer.get("skip"):
        for s in subs:
            results.append(
                {"question": _label(s), "source": "skipped",
                 "option_index": None, "answer": ""}
            )
    else:
        answers = answer.get("answers")
        if not isinstance(answers, list):
            # 旧形单答（外部渠道/纯文本回复）打在第一题上，其余记 skipped
            answers = [
                {
                    "answer": str(answer.get("answer") or ""),
                    "option_index": None,
                    "skip": bool(answer.get("skip")),
                }
            ]
        for i, s in enumerate(subs):
            a = answers[i] if i < len(answers) and isinstance(answers[i], dict) else {}
            opts: list[str] = s["options"]
            # 缺失/空条目（旧形短回复没覆盖到的题）= 该题 skipped
            if a.get("skip") or (not str(a.get("answer") or "").strip() and a.get("option_index") is None):
                results.append(
                    {"question": _label(s), "source": "skipped",
                     "option_index": None, "answer": ""}
                )
                continue
            text = str(a.get("answer") or "").strip()
            idx = a.get("option_index")
            if isinstance(idx, int) and 0 <= idx < len(opts):
                results.append(
                    {"question": _label(s), "source": "option",
                     "option_index": idx, "answer": opts[idx],
                     "free_text": text if text and text != opts[idx] else ""}
                )
            else:
                results.append(
                    {"question": _label(s), "source": "free_text",
                     "option_index": None, "answer": text, "free_text": ""}
                )
    return json.dumps(
        {"ok": True, "multi": True, "results": results}, ensure_ascii=False
    )


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
        question: str = "",
        options: list[str] | str | None = None,
        header: str = "",
        allow_free_text: bool = True,
        questions: list[dict] | list[str] | None = None,
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
        ``options``   — 2-5 user-facing choices, as a REAL JSON ARRAY OF PLAIN
                        STRINGS (never objects — a {"label", "description"}
                        entry has its label extracted, but the description is
                        lost). Omit entirely for a pure free-text question.
                        Each entry must be a complete, self-explanatory label
                        ("安装到 <repo>/.claude/skills（Claude Code 读取）"),
                        because that exact string is what the user clicks and
                        what you get back. Do NOT pass a JSON-encoded string of
                        an array (it is accepted, but a real array is clearer).
                        Never write the choices as "1 / 2 / 3" inside
                        ``question`` — put them here so they become buttons.
        ``header``    — 2-4 word card title, e.g. "选择安装位置".
        ``allow_free_text`` — also offer an "其他" field (default true).
        ``questions`` — SEVERAL independent questions in ONE ask (2-5 entries,
                        each {"question", "options", "header",
                        "allow_free_text"}; a bare string entry is a pure
                        free-text question; ``options`` follows the same
                        plain-string rule above). Each question renders its own
                        clickable options and the user answers all of them in
                        one card. Whenever you have 2+ separate questions USE
                        THIS — do NOT stuff them into one ``question`` body
                        with lettered sub-options in prose, and do NOT
                        synthesize combination options ("A+B", "A+C") from
                        their cross-product: each question must be answerable
                        on its own. ``question`` then serves as the card's
                        overall lead-in (optional).

        Returns JSON: {ok, skipped, source, option_index, answer, free_text}
        where source is "option" | "free_text" | "skipped". When ``skipped``
        is true the user declined to choose — proceed with your best
        judgement and SAY WHICH ASSUMPTION YOU MADE in your reply, so it can
        be corrected. Multi-question mode returns {ok, multi, results:
        [{question, source, option_index, answer}, …]} instead — one entry
        per question, same semantics, empty ``answer`` = that one skipped.
        """
        # ---- 多问题规整（2026-10-29 e48be8a3 实测：3 个问题塞一个 question，
        # 选项做成 2×2×2 组合套餐——第 2/3 题的选项只剩文字）。每个子问题
        # 独立规整；恰 1 条时折回单问题契约（旧路径零改动）。----
        subs: list[dict] = []
        if isinstance(questions, list):
            for ent in questions:
                if isinstance(ent, str):
                    ent = {"question": ent}
                if not isinstance(ent, dict):
                    continue
                sq = str(ent.get("question") or "").strip()
                if sq:
                    subs.append(
                        {
                            "question": sq,
                            "options": normalize_options(ent.get("options")),
                            "header": str(ent.get("header") or "").strip(),
                            "allow_free_text": bool(ent.get("allow_free_text", True)),
                        }
                    )
            if len(subs) > 5:
                return t(
                    "[error] questions accepts at most 5 entries — split "
                    "into separate ask_user calls.",
                    "[error] questions 最多 5 条——请拆成多次 ask_user。",
                )
        if len(subs) == 1:
            # 单条 → 折回单问题：语义等价，复用旧 payload/回执形状。
            q = subs[0]["question"] if not (question or "").strip() else (question or "").strip()
            opts = subs[0]["options"] or normalize_options(options)
            header = (header or "").strip() or subs[0]["header"]
            allow_free_text = allow_free_text and subs[0]["allow_free_text"]
            subs = []
        elif len(subs) >= 2:
            q = (question or "").strip()  # 卡片引子（可空）
            opts = []
        else:
            q = (question or "").strip()
            opts = normalize_options(options)
        if not q and not subs:
            return "[error] question is required"

        if not _INTERACTIVE.get():
            return t(
                "[error] This is an autonomous continuation (unattended) turn — "
                "nobody can answer. Pick the most reasonable default and "
                "continue, and state clearly in your reply which assumption you "
                "made so the user can correct it.",
                "[error] 当前是自主续跑（无人值守）回合，没有人能回答。"
                "请选择最合理的默认继续，并在回复中明确写出你选了什么，以便用户纠正。")

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
                return t(
                    f"[error] Already asked the user {budget['n']} times this "
                    f"turn (limit {max_asks}). Decide yourself, and state your "
                    "assumption in your reply.",
                    f"[error] 本轮已询问用户 {budget['n']} 次（上限 {max_asks}）。"
                    "请自行决定，并把你的假定写进回复。")
            budget["n"] += 1

        payload = {
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
        if subs:
            # 多问题（契约 v2）：questions 数组逐题携带 options；单问题保持
            # 旧形状（旧前端/旧 checkpoint 的重放零感知）。
            payload["multi"] = True
            payload["questions"] = subs
        answer = interrupt(payload)

        if subs:
            return _multi_receipt(subs, answer)

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