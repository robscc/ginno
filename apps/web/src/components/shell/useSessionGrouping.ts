"use client";

import { hiddenSessionIds } from "@/lib/sessionVisibility";
import type { FolderEntry, SessionMeta, WorkflowRun } from "@/lib/types";

// P1-1 自动归档：归档标记只有在该会话「新鲜度」过窗后才真正生效——7 天内有
// 新活动（updated 被 turn 顶上去了）的会话即使服务端 archived 还是 true，也
// 照常出现在正常分组里，等服务端回写 false。
export const SIDEBAR_ACTIVE_WINDOW = 7 * 86400;
// 「一键归档」只碰超过 14 天没动静、且还没归档的非置顶会话。
export const ARCHIVE_ALL_AFTER = 14 * 86400;

export type TimeGroup = "Today" | "Yesterday" | "Last7" | "Earlier";

export interface SessionGroupingInput {
  sessions: SessionMeta[];
  workflowRuns: WorkflowRun[];
  /** C+ 方案③：侧栏按 agent 筛选会话（null = 全部）。 */
  agentFilter: string | null;
  folders: FolderEntry[] | null;
  groupView: "time" | "project";
  /** 归档区多选：普通选中集合（只有落在归档子集里的才归 restore/delete 用）。 */
  selected: Set<string>;
  /** 项目视图里「未分组」的本地化展示名。 */
  ungroupedLabel: string;
}

export interface SessionGrouping {
  hiddenIds: Set<string>;
  visibleSessions: SessionMeta[];
  childrenOf: Map<string, SessionMeta[]>;
  runsOf: Map<string, WorkflowRun[]>;
  descendantCount: (id: string) => number;
  sortedSessions: SessionMeta[];
  pinnedSessions: SessionMeta[];
  unpinnedSessions: SessionMeta[];
  isArchived: (s: SessionMeta) => boolean;
  activeUnpinned: SessionMeta[];
  archivedSessions: SessionMeta[];
  selectedArchivedIds: string[];
  archivedMonthGroups: Array<[string, SessionMeta[]]>;
  sessionGroups: Array<[TimeGroup, SessionMeta[]]>;
  projectGroups: Array<[string, SessionMeta[]]>;
  archiveCandidates: SessionMeta[];
}

/**
 * 侧栏的分组派生数据（纯计算，无状态）：agent 筛选 → 隐藏子树过滤 → 子会话/
 * 运行挂载 → 置顶/天分组/项目分组/归档分组。原先是 AppShell 渲染体内的一段
 * 内联推导，抽出来只为收窄壳文件；语义与求值顺序保持不变。
 */
export function useSessionGrouping({
  sessions,
  workflowRuns,
  agentFilter,
  folders,
  groupView,
  selected,
  ungroupedLabel,
}: SessionGroupingInput): SessionGrouping {
  // Sidebar sessions: activity-day groups, newest activity first. `updated`
  // is bumped per turn server-side, so it tracks last use, not creation.
  // C+ 方案③：agent 筛选作用于分组前的列表，分组/排序逻辑不变。
  //
  // subagent 树（subagent-design.md §6.1）：先按活动日分主行，再把子会话
  // （type==="subagent"）按 parent_session_id 挂到父行下嵌套渲染。子会话不进
  // 天分组——它的活动不应顶起父会话的排序（设计文档开放问题 2 的 UI 侧答案）。
  // 影子/团队空间会话与其整条子树一并隐藏（sessionVisibility）：定时任务拉起
  // 的 subagent 父行是隐藏的影子会话，若走下面孤儿回退会漏进主侧栏。
  const hiddenIds = hiddenSessionIds(sessions);
  const visibleSessions = sessions.filter(
    (s) => !hiddenIds.has(s.id) && (!agentFilter || s.agent_id === agentFilter),
  );
  // 父 id → 直属子会话（按创建时间倒序——最新的在最上，与主列表
  // updated 倒序的阅读方向一致；2026-10-12 用户定稿）。状态分节在渲染
  // 层保序，即每节内也是新→旧。
  const childrenOf = new Map<string, SessionMeta[]>();
  for (const s of visibleSessions) {
    if ((s.type !== "subagent" && s.type !== "delegation") || !s.parent_session_id) continue;
    const list = childrenOf.get(s.parent_session_id) ?? [];
    list.push(s);
    childrenOf.set(s.parent_session_id, list);
  }
  for (const list of childrenOf.values()) {
    list.sort((a, b) => (b.created ?? 0) - (a.created ?? 0));
  }
  // workflow 运行伪条目：把 present_in_session_id 指向可见父会话的运行挂在
  // 父行下（孤儿运行/被筛选掉的父会话不渲染），按开始时间正序，与子会话行
  // 的排列方式一致。
  const runsOf = new Map<string, WorkflowRun[]>();
  for (const r of workflowRuns) {
    if (!r.present_in_session_id) continue;
    if (!visibleSessions.some((p) => p.id === r.present_in_session_id)) continue;
    const list = runsOf.get(r.present_in_session_id) ?? [];
    list.push(r);
    runsOf.set(r.present_in_session_id, list);
  }
  for (const list of runsOf.values()) {
    list.sort((a, b) => a.started - b.started);
  }
  // 全部后代数（嵌套子代理的 +N 尾标与级联删除确认文案共用）。
  const descendantCount = (id: string): number => {
    let n = 0;
    const walk = (pid: string) => {
      for (const c of childrenOf.get(pid) ?? []) {
        n++;
        walk(c.id);
      }
    };
    walk(id);
    return n;
  };
  // 父在本列表可见（未被筛选掉/未删除）的子会话才嵌套；孤儿子会话退回
  // 天分组顶层渲染，避免凭空消失。
  const hasVisibleParent = (s: SessionMeta) =>
    (s.type === "subagent" || s.type === "delegation") &&
    !!s.parent_session_id &&
    visibleSessions.some((p) => p.id === s.parent_session_id);
  const sortedSessions = [...visibleSessions]
    .filter((s) => !hasVisibleParent(s))
    .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || (b.updated ?? 0) - (a.updated ?? 0));
  // 显式置顶的会话独立成组，排在天分组之上，不受活动日分组影响。
  const pinnedSessions = sortedSessions.filter((s) => s.pinned);
  const unpinnedSessions = sortedSessions.filter((s) => !s.pinned);
  // P1-1 自动归档：archived=true 且超过 7 天没活动才算归档；新 turn 会把
  // updated 顶上来，于是它立刻回到正常分组（不必等服务端翻标记）。
  const nowSec = Math.floor(Date.now() / 1000);
  const isArchived = (s: SessionMeta) =>
    s.archived === true && nowSec - (s.updated ?? 0) > SIDEBAR_ACTIVE_WINDOW;
  const activeUnpinned = unpinnedSessions.filter((s) => !isArchived(s));
  const archivedSessions = unpinnedSessions.filter(isArchived);
  // 归档区多选：普通行同样可勾选，但「恢复所选/删除所选」只针对归档子集。
  const selectedArchivedIds = [...selected].filter((id) => {
    const s = sessions.find((x) => x.id === id);
    return s ? isArchived(s) : false;
  });
  // 归档列表按 YYYY-MM 分组（updated 的本地月份），最新月份在前。
  const archivedMonthGroups: Array<[string, SessionMeta[]]> = (() => {
    const by = new Map<string, SessionMeta[]>();
    for (const s of archivedSessions) {
      const d = new Date((s.updated ?? s.created ?? 0) * 1000);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
      const list = by.get(key) ?? [];
      list.push(s);
      by.set(key, list);
    }
    return [...by.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1));
  })();
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const todayMs = dayStart(new Date());
  const groupOf = (s: SessionMeta): TimeGroup => {
    const day = dayStart(new Date((s.updated ?? s.created) * 1000));
    if (day >= todayMs) return "Today";
    if (day >= todayMs - 86400000) return "Yesterday";
    // 2–7 天：仍算「最近」，8 天以上落到 Earlier（再交给归档判断）。
    if (day >= todayMs - SIDEBAR_ACTIVE_WINDOW * 1000) return "Last7";
    return "Earlier";
  };
  const sessionGroups: Array<[TimeGroup, SessionMeta[]]> = [
    ["Today", activeUnpinned.filter((s) => groupOf(s) === "Today")],
    ["Yesterday", activeUnpinned.filter((s) => groupOf(s) === "Yesterday")],
    ["Last7", activeUnpinned.filter((s) => groupOf(s) === "Last7")],
    ["Earlier", activeUnpinned.filter((s) => groupOf(s) === "Earlier")],
  ];

  // P1-2 项目视图：按 primary_folder 对应的文件夹名分组（名字来自 listFolders
  // 缓存）；没挂载或库里已不存在的 id 归「未分组」。activeUnpinned 已按活动
  // 倒序，Map 的插入顺序因此就是「最近有活动的项目在前」，未分组永远垫底。
  const folderNames = new Map((folders ?? []).map((f) => [f.id, f.name || f.path || f.id]));
  const projectGroups: Array<[string, SessionMeta[]]> = (() => {
    if (groupView !== "project") return [];
    const byName = new Map<string, SessionMeta[]>();
    for (const s of activeUnpinned) {
      const name = s.primary_folder ? folderNames.get(s.primary_folder) : undefined;
      const key = name || ungroupedLabel;
      const list = byName.get(key) ?? [];
      list.push(s);
      byName.set(key, list);
    }
    const entries = [...byName.entries()];
    const ungroupedAt = entries.findIndex(([name]) => name === ungroupedLabel);
    if (ungroupedAt >= 0) entries.push(...entries.splice(ungroupedAt, 1));
    return entries;
  })();

  // P1-1 一键归档的候选：Earlier 里超过 14 天无活动、还没归档的非置顶会话。
  const archiveCandidates = activeUnpinned.filter(
    (s) => s.archived !== true && nowSec - (s.updated ?? 0) > ARCHIVE_ALL_AFTER,
  );

  return {
    hiddenIds,
    visibleSessions,
    childrenOf,
    runsOf,
    descendantCount,
    sortedSessions,
    pinnedSessions,
    unpinnedSessions,
    isArchived,
    activeUnpinned,
    archivedSessions,
    selectedArchivedIds,
    archivedMonthGroups,
    sessionGroups,
    projectGroups,
    archiveCandidates,
  };
}
