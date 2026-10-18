import type { SessionMeta } from "@/lib/types";

/**
 * 会话可见性（侧栏 / ⌘K 搜索共用的单一来源）。
 *
 * 影子会话整类隐藏（scheduled-tasks-design §3.6/§10 决议 5），团队会话只活
 * 在 /team 空间（P2 产品重构 2026-10-13）。它们的**子会话**（subagent /
 * delegation）随父级一并隐藏：定时任务拉起的 subagent，parent 指向的是被
 * 隐藏的影子会话——父行不可见是设计使然而非删除，不能走 AppShell 孤儿
 * 子会话的顶层回退（那会让影子子树漏进「我的对话」）。
 *
 * 隐藏是传递的：subagent 之下再嵌 subagent（depth 0/1/2）整条链都跟着
 * 父级走。parent 已删除的孤儿子会话不在此列——它们仍由调用方按原规则
 * 回退渲染。
 */
const HIDDEN_TYPES: ReadonlySet<NonNullable<SessionMeta["type"]>> = new Set([
  "scheduled",
  "team",
  "team-member",
]);

export function hiddenSessionIds(sessions: SessionMeta[]): Set<string> {
  const byParent = new Map<string, SessionMeta[]>();
  const hidden = new Set<string>();
  for (const s of sessions) {
    if (s.parent_session_id) {
      const list = byParent.get(s.parent_session_id);
      if (list) list.push(s);
      else byParent.set(s.parent_session_id, [s]);
    }
    if (s.type && HIDDEN_TYPES.has(s.type)) hidden.add(s.id);
  }
  let frontier = [...hidden];
  while (frontier.length) {
    const next: string[] = [];
    for (const pid of frontier) {
      for (const c of byParent.get(pid) ?? []) {
        if (c.id && !hidden.has(c.id)) {
          hidden.add(c.id);
          next.push(c.id);
        }
      }
    }
    frontier = next;
  }
  return hidden;
}
