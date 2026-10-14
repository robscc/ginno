"""Team lead tools: list_members / deliver_to_member / assign_task
(agent-team-design.md §5.3.2 / §6 P3，2026-10-14 实施)。

只绑在**团队主会话**（type="team"）上——Lead 自主分派的落点；普通会话与
成员工作台完全不注册（成员互通是 §5.2 要点 3 明确推迟的）。

§5.9 纪律：工具层**不复制状态/投递逻辑**——list_members 读
``teams.status``（与成员面板同一个服务端真值），deliver/assign 直接调
``teams.deliver.deliver``（与 HTTP API 同一函数）。herdr 能做到「agent 等
到另一个 agent 真的 blocked」，靠的正是这个单一真值。

权限：照 spawn_subagent / delegate_agent 先例，**不进**豁免集——默认 ask，
用户批准的正是「Lead 要分派这件事」本身。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from ..lang import t

TEAM_TOOL_NAMES = {"list_members", "deliver_to_member", "assign_task"}

_LIST_DOC_EN = """List this team's member instances: instance_id, display name,
        agent, semantic status (blocked/unknown/error/working/waiting/done/idle)
        and a human-readable detail line. This reads the SAME server-side status
        the user's team panel reads — it is authoritative, never guessed.

        With wait=true it instead blocks until the target members (default: all)
        reach a settled status, then returns their statuses in one go. Settled
        means: blocked / unknown / error / done / idle (working and waiting are
        still producing). timeout_s > 0 returns a marked partial at the deadline;
        the user stopping your turn interrupts the wait.

        Args:
            wait: false = list immediately (default); true = block until the
                target members settle.
            until: With wait=true: statuses that end the wait (default: all
                settled statuses).
            ids: With wait=true: instance_ids to wait for; empty = all members.
            timeout_s: Max seconds to wait (wait=true only); 0 = until settled.
        """

_DELIVER_DOC_EN = """Deliver a plain message to one team member instance (same
        channel the user's @-mention uses). The message lands in that member's
        independent session; when its turn finishes, a reply summary is posted
        back into this conversation automatically — you do NOT need to poll,
        and you do NOT need list_members(wait=true) after delivering unless the
        user explicitly asked for a synchronous wait.

        Returns a three-state receipt: delivered (verifiable evidence: steer
        queue entry / turn started / queue slot) · stalled (sent but the member
        did not enter working — do NOT auto-resend, inspect instead) · failed
        (definitely not sent, e.g. the member is blocked on a permission card —
        tell the user to answer that card first).

        Args:
            instance_id: The member instance to deliver to (preferred addressing
                — see list_members). Empty with agent_id resolves to that
                agent's first instance in this team.
            agent_id: Alternative addressing by agent id (ignored when
                instance_id is given).
            text: The message text (required, non-empty).
        """

_ASSIGN_DOC_EN = """Assign a self-contained task to one team member instance:
        like deliver_to_member but structured — the goal/constraints/acceptance
        you write become the task brief the member sees (it cannot see this
        conversation, so write for a capable but context-free colleague).
        The reply summary comes back automatically when the member finishes.

        Args:
            goal: The complete, self-contained task description (required).
            constraints: Boundary rules (read-only scope, files not to touch,
                time budget…), may be empty.
            acceptance: Acceptance criteria to judge the result against, may be
                empty.
            instance_id: Target member instance (preferred; see list_members).
            agent_id: Alternative addressing by agent id.
        """


def build_team_tools(session_id: str | None) -> list:
    """The lead toolset for ONE session — bound only to a team MAIN session
    (meta type == "team"); everyone else gets []. The team itself is resolved
    per call (team_of_session), so a roster rebuilt/renamed around the session
    keeps working without a graph rebuild."""
    if not session_id:
        return []
    from ..session_meta import _find_meta
    from ..teams import store as team_store
    from ..teams import wait as team_wait

    found = _find_meta(session_id)
    if not found or (found[0].get("type") or "") != "team":
        return []

    def _team() -> tuple[str, dict | None]:
        """(team_id, team) of THIS session's team; ("", None) when it is no
        longer a roster main (disbanded / orphaned)."""
        located = team_store.team_of_session(session_id)
        if located is None or located[1] is not None:
            # 不是任何团队的主会话（成员命中也算不合法——一切经主会话）。
            return "", None
        return str(located[0].get("id") or ""), located[0]

    async def list_members(
        wait: bool = False,
        until: list[str] | None = None,
        ids: list[str] | None = None,
        timeout_s: int = 0,
    ) -> str:
        """列出本团队的成员实例：instance_id、显示名、agent、语义态
        （blocked/unknown/error/working/waiting/done/idle）与展示说明。
        读的是与用户团队面板**同一个服务端状态**——权威，不是猜测。

        wait=true 时改为阻塞，直到目标成员（默认全部）到达「已安定」态，
        一次性返回各自状态。已安定 = blocked/unknown/error/done/idle
        （working/waiting 仍在产出，不算）。timeout_s > 0 到点返回 partial；
        用户停止你的回合会中断等待。

        Args:
            wait: false = 立即列出（默认）；true = 阻塞等待目标成员安定。
            until: wait=true 时终止等待的状态列表（默认：全部已安定态）。
            ids: wait=true 时要等待的 instance_id 列表；空 = 全部成员。
            timeout_s: 最长等待秒数（仅 wait=true）；0 = 直到安定。
        """
        team_id, team = _team()
        if not team_id:
            return t("[error] This session is no longer a team main session.",
                     "[error] 本会话已不是任何团队的主会话。")
        if wait:
            return await team_wait.wait_for_members(
                session_id, team_id, until=until, ids=ids, timeout_s=timeout_s
            )
        from ..teams import status as team_status

        team_status.refresh_team(team_id)
        rows = team_status.team_payload(team_store.get_team(team_id) or team)[
            "member_rows"
        ]
        slim = [
            {k: r.get(k) for k in (
                "instance_id", "display_name", "agent_id",
                "role", "status", "status_detail",
            )}
            for r in rows
        ]
        return json.dumps({"count": len(slim), "members": slim}, ensure_ascii=False)

    async def _resolve_target(instance_id: str, agent_id: str) -> tuple[str, str]:
        """(instance_id, error)。两者都空或都解析不到 → error 非空。"""
        team_id, _team = _team()
        if not team_id:
            return "", t("[error] This session is no longer a team main session.",
                         "[error] 本会话已不是任何团队的主会话。")
        iid = (instance_id or "").strip()
        if not iid:
            aid = (agent_id or "").strip()
            if not aid:
                return "", t("[error] instance_id or agent_id is required.",
                             "[error] 需要提供 instance_id 或 agent_id。")
            target = team_store.member_of_agent(team_id, aid)
            if target is None:
                return "", t(
                    f"[error] '{aid}' is not a member of this team.",
                    f"[error] 「{aid}」不是本团队成员。")
            iid = str(target.get("instance_id"))
        return iid, ""

    async def deliver_to_member(
        instance_id: str = "",
        agent_id: str = "",
        text: str = "",
    ) -> str:
        """给一个成员实例投递一条普通消息（与用户 @ 成员同一条通道）。
        消息进入该成员的独立 session；其 turn 结束后回帖摘要会**自动**回到
        本对话——不需要轮询，投递后也不需要 list_members(wait=true)，除非
        用户明确要求同步等待。

        回执三态：delivered（有可验证证据）/ stalled（发了但成员没进入
        working——**不要自动重发**，先查看）/ failed（确定没发，例如成员
        blocked 在权限卡上——应告知用户先答复那张卡）。

        Args:
            instance_id: 目标成员实例（首选寻址，见 list_members）。留空且
                给了 agent_id 时解析为该 agent 在本团队的第一个实例。
            agent_id: 按 agent 寻址的备选方式（给了 instance_id 时忽略）。
            text: 消息正文（必填，非空）。
        """
        from ..teams import deliver as team_deliver

        team_id, _team = _team()
        if not team_id:
            return t("[error] This session is no longer a team main session.",
                     "[error] 本会话已不是任何团队的主会话。")
        iid, err = await _resolve_target(instance_id, agent_id)
        if err:
            return err
        result = await team_deliver.deliver(team_id, iid, text or "")
        return json.dumps(result, ensure_ascii=False)

    async def assign_task(
        goal: str = "",
        constraints: str = "",
        acceptance: str = "",
        instance_id: str = "",
        agent_id: str = "",
    ) -> str:
        """给一个成员实例分派自足任务：与 deliver_to_member 同通道，但
        结构化——你写的 goal/constraints/acceptance 组成任务简报（成员
        看不到本对话，按「有能力但完全无上下文的同事」来写）。完成后
        回帖摘要自动回传。

        Args:
            goal: 自足的完整任务描述（必填）。
            constraints: 边界规则（只读范围、不许动的文件、时间预算…），可为空。
            acceptance: 验收标准，供收到结果后对照判定，可为空。
            instance_id: 目标成员实例（首选寻址；见 list_members）。
            agent_id: 按 agent 寻址的备选方式。
        """
        from ..teams import deliver as team_deliver

        team_id, _team = _team()
        if not team_id:
            return t("[error] This session is no longer a team main session.",
                     "[error] 本会话已不是任何团队的主会话。")
        goal = (goal or "").strip()
        if not goal:
            return t("[error] goal is required.", "[error] goal 必填。")
        iid, err = await _resolve_target(instance_id, agent_id)
        if err:
            return err
        brief = t("[task] Goal: ", "[任务] 目标：") + goal
        if (constraints or "").strip():
            brief += "\n" + t("Constraints: ", "约束：") + constraints.strip()
        if (acceptance or "").strip():
            brief += "\n" + t("Acceptance criteria: ", "验收标准：") + acceptance.strip()
        result = await team_deliver.deliver(team_id, iid, brief)
        return json.dumps(result, ensure_ascii=False)

    # 语言先解析再装饰（同 build_subagent_tools 的约定：langchain 在装饰
    # 时解析 __doc__，这里每次建工具集时跟随设置语言）。
    list_members.__doc__ = t(_LIST_DOC_EN, list_members.__doc__)
    deliver_to_member.__doc__ = t(_DELIVER_DOC_EN, deliver_to_member.__doc__)
    assign_task.__doc__ = t(_ASSIGN_DOC_EN, assign_task.__doc__)
    return [tool(list_members), tool(deliver_to_member), tool(assign_task)]
