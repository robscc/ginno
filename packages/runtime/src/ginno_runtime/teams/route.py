"""协作广场消息路由（agent-team-design 路由模块，2026-10-14 用户定稿）。

团队主会话（协作广场）里用户发消息时，由本模块决定**谁来响应**：

* 有 @ 成员 → 被 @ 的成员响应（调用方 ws.py 的 @ 投递分支；本模块的
  mentions 入参即已解析的 @ 目标，为后续「一条消息 @ 多人」预留统一入口）；
* 无 @ → **默认路由给领队**（leader 成员实例——与「领队实体化」模型闭环：
  广场不承担对话功能，跟广场说话 = 跟领队对话，落在领队自己的会话里）；
  无领队实例 → **群聊广播**：路由给全部成员（广场=群聊语义的兜底）；
  空团队 → 空数组，调用方提示先加成员（主会话自身永远不起 turn）。

返回值是**成员 instance_id 数组**：后续演进（意图分类路由、多人并行响应、
负载分担）都只改本模块，调用方契约不变——数组即「可多响应」的预留。
"""

from __future__ import annotations

from . import store as team_store


def route_targets(
    team: dict,
    mentioned_instance_ids: list[str] | None = None,
) -> list[str]:
    """决定响应成员。返回 instance_id 数组（可能为空 = 主会话自身兜底）。

    v1 规则：mentions 非空 → 原样返回（去重保序）；为空 → [leader]。
    """
    if mentioned_instance_ids:
        seen: list[str] = []
        for iid in mentioned_instance_ids:
            if iid and iid not in seen:
                seen.append(iid)
        return seen
    # 领队判定双保险：leader_instance_id（API 层维护的冗余）或任一
    # role==leader 的成员（store 层事实）。无领队 → 广播全部成员（群聊）；
    # 空团队 → 空数组（调用方提示先加成员——主会话永不自跑 turn）。
    leader = str(team.get("leader_instance_id") or "")
    members = team_store.list_members(str(team.get("id") or ""))
    if not any(m.get("instance_id") == leader for m in members):
        leader = next(
            (str(m["instance_id"]) for m in members if m.get("role") == "leader"),
            "",
        )
    if leader:
        return [leader]
    return [str(m["instance_id"]) for m in members if m.get("instance_id")]
