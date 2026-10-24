"use client";

import { useTranslations } from "next-intl";
import type { SessionMeta } from "@/lib/types";
import type { TimeGroup } from "@/components/shell/useSessionGrouping";

export interface SidebarSessionListProps {
  pinnedSessions: SessionMeta[];
  sessionGroups: Array<[TimeGroup, SessionMeta[]]>;
  projectGroups: Array<[string, SessionMeta[]]>;
  groupView: "time" | "project";
  /** 天分组标题的本地化展示名（内部 key 保持英文）。 */
  groupLabel: Record<TimeGroup, string>;
  childrenOf: Map<string, SessionMeta[]>;
  renderSessionRow: (
    s: SessionMeta,
    depth?: number,
    childRows?: SessionMeta[],
    dimmed?: boolean,
  ) => React.ReactNode;
  ready: boolean;
  sessionsCount: number;
  sortedCount: number;
  sessionError: string | null;
  onOpenSessionError: () => void;
  /** 已归档区插槽：DOM 上位于分组之后、空态之前（与原先一致）。 */
  children?: React.ReactNode;
}

/**
 * 侧栏会话列表主体：置顶组 → 天分组/项目分组 → （已归档插槽）→ 首屏骨架/
 * 空态/会话错误。分组标签与空态文案仍在 shell 命名空间下。
 */
export function SidebarSessionList({
  pinnedSessions,
  sessionGroups,
  projectGroups,
  groupView,
  groupLabel,
  childrenOf,
  renderSessionRow,
  ready,
  sessionsCount,
  sortedCount,
  sessionError,
  onOpenSessionError,
  children,
}: SidebarSessionListProps) {
  const t = useTranslations("shell");
  return (
    <>
      {/* sessions grouped by activity day（置顶组在最上） */}
      {pinnedSessions.length > 0 && (
        <div className="mb-1">
          <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">
            {t("groups.pinned")}
          </div>
          <div className="space-y-0.5">
            {pinnedSessions.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
          </div>
        </div>
      )}
      {groupView === "time"
        ? sessionGroups.map(([label, rows]) =>
            rows.length ? (
              <div key={label} className="mb-1">
                <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">
                  {groupLabel[label]}
                </div>
                <div className="space-y-0.5">
                  {rows.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
                </div>
              </div>
            ) : null,
          )
        : projectGroups.map(([name, rows]) => (
            <div key={name} className="mb-1">
              <div className="px-2.5 pb-1 pt-3 text-[11px] font-medium text-faint">{name}</div>
              <div className="space-y-0.5">
                {rows.map((s) => renderSessionRow(s, 0, childrenOf.get(s.id) ?? []))}
              </div>
            </div>
          ))}

      {children}

      {/* 首屏骨架：ready 之前 sessions 还是空数组，直接渲染空态会在每次冷启动/
          刷新时闪一下「还没有会话」（SSR 也含这句文案）。 */}
      {!ready && (
        <div className="space-y-2 px-2.5 py-3" aria-hidden>
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex items-center gap-2">
              <span className="h-3.5 w-3.5 shrink-0 animate-pulse rounded bg-line2/70" />
              <span className="h-3 flex-1 animate-pulse rounded bg-line2/50" />
            </div>
          ))}
        </div>
      )}
      {ready && sessionsCount === 0 && (
        <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
          {t("empty.noSessions")}
          <br />
          {t("empty.noSessionsHint")}
        </div>
      )}
      {/* C+ 方案③：有会话但当前筛选下为空——独立兜底文案，避免误以为没有会话 */}
      {ready && sessionsCount > 0 && sortedCount === 0 && (
        <div className="px-2.5 py-2 text-xs leading-relaxed text-faint">
          {t("empty.filtered")}
          <br />
          {t("empty.filteredHint")}
        </div>
      )}

      {sessionError && (
        <button
          onClick={onOpenSessionError}
          title={t("session.errorHint")}
          className="mx-1 mb-3 block rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-left text-[11px] leading-snug text-yellow hover:bg-yellow/15"
        >
          {sessionError}
        </button>
      )}
    </>
  );
}
