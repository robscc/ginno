// openMemberIntent — 跨路由的「进入成员工作台」意图（backlog P2-25）。
// TeamReplyCard 可能出现在任何渲染了团队主会话的路由；纯 window 事件只有
// /team 的 TeamShell 在监听，其它路由点「进入工作台」会静默丢失。现在：
// 不在 /team 时先把意图存进 sessionStorage 再 router.push("/team)，
// TeamShell 挂载时消费（一次性）。用 sessionStorage 而非 localStorage：
// 意图是一次性导航状态，不该跨应用重启存活。
//
// 独立小模块（不 import 任何组件）：TeamReplyCard 与 TeamShell 都引用它，
// 避免 TeamShell ↔ TeamReplyCard 经 ChatStream/TranscriptList 形成循环。

export interface OpenMemberIntent {
  teamId?: string;
  instanceId: string;
  sessionId: string;
}

const KEY = "ginno-team-open-member-intent";

export function stashOpenMemberIntent(intent: OpenMemberIntent): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(intent));
  } catch {
    /* 存储不可用——同路由的 window 事件路径仍然有效 */
  }
}

/** 读取并立即清除（一次性消费）；解析失败按无意图处理。 */
export function consumeOpenMemberIntent(): OpenMemberIntent | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    sessionStorage.removeItem(KEY);
    const p = JSON.parse(raw) as Partial<OpenMemberIntent> | null;
    return p && p.instanceId && p.sessionId
      ? { teamId: p.teamId, instanceId: p.instanceId, sessionId: p.sessionId }
      : null;
  } catch {
    return null;
  }
}
