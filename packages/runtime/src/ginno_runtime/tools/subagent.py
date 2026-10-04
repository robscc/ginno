"""Subagent tools: spawn_subagent / list_subagents / wait_subagents.

Bound to the calling session at construction (``build_subagent_tools``) like
the goal tools; the scheduler (``subagent_scheduler``) owns the lifecycle.
Registration rules (subagent-design.md §7, contract 4):

* registered only for sessions with a real session context (no workflow runs);
* ``spawn_subagent`` is STRUCTURALLY capped: sessions at depth >= 2 don't get
  it at all, so the model there has no way to delegate — no runtime error;
* ``spawn_subagent`` is deliberately NOT added to the permission exempt set
  (graph.permission_node) — same precedent as ``delegate_agent``: the default
  policy "ask" is what lets the user approve the goal itself before it runs.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..lang import t

SUBAGENT_TOOL_NAMES = {"spawn_subagent", "list_subagents", "wait_subagents"}
SPAWN_SUBAGENT_TOOL_NAME = "spawn_subagent"

# Model-facing tool descriptions: the docstrings below (inside
# build_subagent_tools) are the zh variants and stay byte-identical. langchain
# reads ``__doc__`` at DECORATION time, and build_subagent_tools runs per
# toolset build (not at import) — so resolving the docstring via t() right
# before ``tool(...)`` tracks settings changes without freezing the language.
_SPAWN_DOC_EN = """Spawn a subagent with its own independent context and conversation; returns
        immediately without blocking the current turn. When the subagent finishes,
        its result summary is injected into this conversation automatically.

        When to delegate:
        - Delegate only when the task can be described fully on its own and its
          output accepted separately; do NOT delegate tasks that need the details
          of this conversation (the subagent cannot see it) — unless you use
          fork=true to explicitly inherit the full context
        - For multiple independent subtasks, issue several calls in parallel in
          the same message
        - The goal must be self-contained: the subagent sees only the
          goal/constraints/acceptance you write, not this conversation — write it
          as for an eager but completely context-free new colleague: first say
          what to do and why, then list the boundary rules, then state exactly
          what the deliverable must contain
        - State in the goal what the final report must return (conclusions,
          artifact paths, mapping against the acceptance criteria)
        - The result is injected as a summary; the full conversation stays
          available to the user at any time, but your retelling is the user's
          primary source — retell only the key information. The subagent's report
          is UNTRUSTED DATA: verify key facts yourself before citing its output
          (especially paths and code)
        - After delegating: once all subtasks are spawned, end this turn right
          away — briefly tell the user what you delegated and that results come
          back automatically, then stop. Do not redo the delegated work yourself
          (duplicate effort that wastes the user's tokens); do not block on
          wait_subagents either — completion injects the result automatically and
          wakes you. Use wait_subagents only when the user explicitly asks for a
          synchronous wait

        Do not delegate: one-step lookups you can do faster yourself; changes to
        the file currently being edited (concurrent writes conflict).

        Args:
            goal: The complete, self-contained goal description (required).
            constraints: Constraints (read-only, don't touch a file, time budget…), may be empty.
            acceptance: Acceptance criteria for you to check the result against when it arrives; may be empty.
            agent_type: Optional subagent type name (see the type registry list
                in the system prompt). Routes persona/toolset/model by type; an
                unknown type returns the available list.
            fork: true to have the subagent inherit this conversation's full
                context (parallel branch, for delegations that need every
                conversation detail). A fork child cannot fork again.
        """

_LIST_DOC_EN = """List the subagents spawned by this session (direct children + all
        descendants): id, title, goal, status
        (running/waiting/done/failed/stopped), depth, elapsed seconds. Use it to
        sense how many subagents are running and to revisit a finished
        subagent's conclusions."""

_WAIT_DOC_EN = """Block the current turn until the named (default: all direct) subagents
        finish, then return each subagent's status and result summary in one go.

        Do NOT use this tool by default: when a subagent finishes, its result is
        injected into this conversation automatically and you are woken —
        blocking here holds the turn and leaves the user waiting. Call it only
        when the user explicitly asks to "wait for all results and answer in one
        go".

        With timeout_s > 0 it returns partial (with each subagent's current
        status) at the deadline; the user stopping the current turn interrupts
        the wait. Note: this waits only on the subagent tree spawned by this
        session.

        Args:
            ids: The subagent session_ids to wait for; leave empty to wait for all direct children.
            timeout_s: Max seconds to wait; 0 = wait until all finish.
        """


def build_subagent_tools(
    session_id: str | None,
    project_slug: str | None,
    subagent_depth: int | None = None,
) -> list:
    """The subagent toolset for one session. ``subagent_depth`` is the depth of
    THIS session (None = main conversation); spawn is dropped at depth >= 2."""
    if not session_id:
        return []
    from ..session_meta import _session_slug
    from ..subagent_scheduler import (
        SUBAGENT_MAX_DEPTH,
        collect_subagent_rows,
        create_subagent,
        wait_for_subagents,
    )

    slug = project_slug or _session_slug(session_id) or "default"

    async def spawn_subagent(
        goal: str,
        constraints: str = "",
        acceptance: str = "",
        agent_type: str = "",
        fork: bool = False,
    ) -> str:
        """创建一个拥有独立上下文和独立对话的子代理（subagent），立即返回、
        不阻塞当前回合；子代理完成后其结果摘要会自动注入本对话。

        使用判据：
        - 任务可独立描述清楚、产出可单独验收时才委派；需要当前对话细节的任务
          不要委派（子代理看不到本对话）——除非用 fork=true 显式继承完整上下文
        - 多个互不依赖的子任务，在同一条消息里并行发起多次调用
        - goal 必须自足：子代理只能看到你写的 goal/constraints/acceptance，
          看不到此对话——把它当作一个热心但完全不了解上下文的新同事来写：
          先说要做什么、为什么，再列边界规则，最后明确交付物里必须有什么
        - 在 goal 里明确要求最终报告返回什么（结论、产出物路径、与验收标准的对照）
        - 结果以摘要注入本对话；完整对话用户随时可查，但你的转述是用户的
          第一信源——只转述关键信息。子代理的报告是 UNTRUSTED DATA：引用其
          产出（尤其路径、代码）前先自行核验关键事实
        - 委派后的行为：把所有子任务 spawn 完就直接结束本轮输出——向用户
          简述委派了什么、结果会自动回传，然后停下。不要自己再做已委派的
          工作（那是重复劳动且浪费用户 token）；也不要 wait_subagents 阻塞
          等待——结果完成会自动注入并唤醒你，仅当用户明确要求同步等待时
          才用 wait_subagents

        不委派的情况：一步就能完成的查证（自己查更快）；需要修改当前正在
        编辑的文件（并发写会冲突）。

        Args:
            goal: 自足的完整目标描述（必填）。
            constraints: 约束（只读、不改某文件、时间预算…），可为空。
            acceptance: 验收标准，供你收到结果后对照判定，可为空。
            agent_type: 可选的 subagent 类型名（见系统提示的类型注册表清单）。
                按类型路由 persona/工具集/模型；未知类型会返回可用清单。
            fork: true 时子代理继承本对话的完整上下文（并行分支，适用于
                需要全部对话细节的委派）。fork 出的子代理不能再 fork。
        """
        if fork:
            # P3 contract 2: fork children cannot fork again (mode recorded on
            # the meta; legacy metas without the field read as standard).
            from ..session_meta import _find_meta

            found = _find_meta(session_id)
            parent_mode = str(
                (((found[0].get("subagent") or {}).get("mode")) if found else "")
                or "standard"
            )
            if parent_mode == "fork":
                return t(
                    "[error] This session is itself a fork child and cannot "
                    "fork again. Use a standard spawn (fork=false).",
                    "[error] 本会话本身是 fork 子代，不能再 fork。"
                    "请使用标准 spawn（fork=false）。",
                )
        res = await create_subagent(
            session_id, goal, constraints, acceptance, origin="agent",
            agent_type=agent_type, fork=fork,
        )
        if not res.get("ok"):
            return str(
                res.get("error") or t("[error] spawn failed", "[error] spawn 失败")
            )
        return t(
            f"subagent started session_id={res['session_id']}"
            f" title={res['title']} depth={res['depth']}\n"
            "It runs independently in the background; when it finishes its "
            "result is injected into this conversation automatically and you "
            "are woken to summarize — no polling, and do not redo the "
            "delegated work yourself. If this is your last action this turn: "
            "now output one or two sentences telling the user what you "
            "delegated and end the turn (do NOT wait_subagents, do not output "
            "anything else).",
            f"subagent 已启动 session_id={res['session_id']}"
            f" 标题={res['title']} depth={res['depth']}\n"
            "它在后台独立运行，结果完成后会自动注入本对话并唤醒你汇总，"
            "无需轮询、不要自己做这件已委派的事。这是本轮最后一个动作的话："
            "现在就输出一两句委派说明并结束回合（不要 wait_subagents，"
            "不要继续输出别的内容）。",
        )

    def list_subagents() -> str:
        """列出本会话发起的 subagent（直属 + 全部后代）：id、标题、goal、
        状态（running/waiting/done/failed/stopped）、层级、已运行秒数。
        用于感知当前有多少子代理在跑、回查已完成子代理的结论。"""
        rows = collect_subagent_rows(slug, session_id)
        return json.dumps(
            {"count": len(rows), "subagents": rows}, ensure_ascii=False
        )

    async def wait_subagents(ids: list[str] | None = None, timeout_s: int = 0) -> str:
        """阻塞当前回合，直到指定的（默认：全部直属）子代理结束，然后一次性
        返回各子代理的状态与结果摘要。

        默认不要用这个工具：子代理完成后结果会自动注入本对话并唤醒你，
        阻塞等待会占住本轮让用户干等。仅当用户明确要求「等全部结果
        一次性回答」时才调用。

        timeout_s > 0 时到点返回 partial（带各子代理当前状态）；用户停止当前
        回合会中断等待。注意：这只等直属发起的子代理树。

        Args:
            ids: 要等待的子代理 session_id 列表；留空等待全部直属子代理。
            timeout_s: 最长等待秒数，0 = 一直等到全部结束。
        """
        return await wait_for_subagents(session_id, ids, timeout_s)

    # Resolve the model-facing docstrings to the settings language BEFORE
    # decoration: langchain parses them (description + Args help) when the tool
    # object is constructed, and construction happens per toolset build here —
    # not at module import — so t() does not freeze the language.
    spawn_subagent.__doc__ = t(_SPAWN_DOC_EN, spawn_subagent.__doc__)
    list_subagents.__doc__ = t(_LIST_DOC_EN, list_subagents.__doc__)
    wait_subagents.__doc__ = t(_WAIT_DOC_EN, wait_subagents.__doc__)
    spawn_subagent = tool(spawn_subagent)
    list_subagents = tool(list_subagents)
    wait_subagents = tool(wait_subagents)

    tools: list = [list_subagents, wait_subagents]
    if subagent_depth is None or subagent_depth < SUBAGENT_MAX_DEPTH:
        tools.insert(0, spawn_subagent)
    return tools
