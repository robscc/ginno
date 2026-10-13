"use client";

import { TeamShell } from "@/components/team/TeamShell";

/** AgentTeam 模式（agent-team-design.md §5.1），入口在侧边栏 footer nav。
 *  AppShell 把非工作区路由装进一个 flex 子项，根节点要自己占满 flex 空间
 *  （同 /workflows、/scheduled 的先例）。 */
export default function TeamPage() {
  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <TeamShell />
    </div>
  );
}
