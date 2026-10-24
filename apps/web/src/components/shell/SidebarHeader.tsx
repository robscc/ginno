"use client";

import { useTranslations } from "next-intl";
import { Archive, Plus, Search } from "lucide-react";
import { agentHex } from "@/lib/theme";
import type { AgentConfig } from "@/lib/types";

export interface SidebarHeaderProps {
  groupView: "time" | "project";
  onGroupViewChange: (v: "time" | "project") => void;
  /** 一键归档候选数（Earlier 里 >14 天无活动且未归档的非置顶会话）。 */
  archiveCandidatesCount: number;
  bulkBusy: boolean;
  onArchiveAll: () => void;
  selectMode: boolean;
  onToggleSelectMode: () => void;
  onNewSession: () => void;
  onSearch: () => void;
  agents: AgentConfig[];
  agentFilter: string | null;
  onAgentFilterChange: (id: string | null) => void;
  /** 会话分组/归档区等滚动内容（放在滚动视口内、主操作与筛选 chips 之后）。 */
  children: React.ReactNode;
}

/**
 * 侧栏顶部：品牌 + 视图/批量工具条，然后是滚动视口内的主操作与会话检索入口、
 * C+ 方案③ 的 agent 筛选 chips。滚动视口本身也在这里开，children 直接接在其
 * 后——保证 aside 内仍是「品牌 → 工具条 → 滚动区 → 提示/批量条 → 页脚」的
 * 同一 DOM 结构。
 */
export function SidebarHeader({
  groupView,
  onGroupViewChange,
  archiveCandidatesCount,
  bulkBusy,
  onArchiveAll,
  selectMode,
  onToggleSelectMode,
  onNewSession,
  onSearch,
  agents,
  agentFilter,
  onAgentFilterChange,
  children,
}: SidebarHeaderProps) {
  const t = useTranslations("shell");
  return (
    <>
      {/* brand */}
      <div className="flex items-center gap-2.5 px-4 pb-2 pt-4">
        <img src="/icon.png" alt="" className="h-7 w-7" />
        <span className="text-[15px] font-semibold tracking-tight">GinnoWork</span>
      </div>

      {/* 侧栏工具条（P1-1/P1-2/P1-4）：时间/项目视图切换、一键归档、多选入口 */}
      <div className="flex items-center gap-1 px-3 pb-2">
        <div className="flex items-center gap-0.5 rounded-md border border-line p-0.5">
          {(["time", "project"] as const).map((v) => (
            <button
              key={v}
              onClick={() => onGroupViewChange(v)}
              className={`rounded px-1.5 py-0.5 text-[11px] transition-colors ${
                groupView === v
                  ? "bg-card2 text-txt"
                  : "text-faint hover:text-txt"
              }`}
            >
              {v === "time" ? t("view.time") : t("view.project")}
            </button>
          ))}
        </div>
        <span className="ml-auto flex items-center gap-1">
          <button
            onClick={onArchiveAll}
            disabled={!archiveCandidatesCount || bulkBusy}
            aria-label={t("bulk.archiveAll")}
            title={
              archiveCandidatesCount
                ? t("bulk.archiveAllHint", { count: archiveCandidatesCount })
                : t("bulk.archiveAllEmpty")
            }
            className="rounded p-1 text-muted transition-colors hover:bg-card2 hover:text-txt disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <Archive className="h-3.5 w-3.5" />
          </button>
          <button
            onClick={onToggleSelectMode}
            title={selectMode ? t("bulk.cancel") : t("bulk.selectHint")}
            className={`rounded px-1.5 py-0.5 text-[11px] transition-colors ${
              selectMode
                ? "bg-card2 text-violet"
                : "text-muted hover:bg-card2 hover:text-txt"
            }`}
          >
            {selectMode ? t("bulk.cancel") : t("bulk.select")}
          </button>
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-2.5 pb-2">
        {/* primary actions first (open-experience prototype) */}
        <div className="mb-1 space-y-0.5 border-b border-line pb-2.5">
          <button onClick={onNewSession} className="nav-item" title={t("nav.newSessionHint")}>
            <Plus className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.newSession")}</span>
            <kbd className="ml-auto shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘N</kbd>
          </button>
          <button onClick={onSearch} className="nav-item">
            <Search className="h-4 w-4 shrink-0" />
            <span className="truncate">{t("nav.searchSessions")}</span>
            <kbd className="ml-auto shrink-0 rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-faint">⌘K</kbd>
          </button>
        </div>

        {/* C+ 方案③：agent 筛选 chips（样式同 composer chip 行，点击已选中的取消） */}
        <div className="flex flex-wrap gap-1 px-1 pb-2 pt-1.5">
          {agents.map((a) => {
            const sel = agentFilter === a.id;
            const hex = agentHex(a.color);
            return (
              <button
                key={a.id}
                onClick={() => onAgentFilterChange(sel ? null : a.id)}
                title={sel ? t("filter.clear") : t("filter.showOnly", { name: a.name })}
                className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
                  sel ? "" : "border-line bg-card text-muted hover:border-line2 hover:text-txt"
                }`}
                style={sel ? { borderColor: hex, background: hex + "1a", color: hex } : undefined}
              >
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
                {a.name}
              </button>
            );
          })}
        </div>

        {children}
      </div>
    </>
  );
}
