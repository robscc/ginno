"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, Search, Trash2 } from "lucide-react";
import * as api from "@/lib/runtime";
import type { SearchMessageHit } from "@/lib/runtime";
import type { SessionMeta } from "@/lib/types";
import { MessageHitRow, SessionHitRow } from "@/components/shell/searchShared";

export interface SidebarArchiveProps {
  archivedSessions: SessionMeta[];
  /** 归档列表按 YYYY-MM 分组（updated 的本地月份），最新月份在前。 */
  archivedMonthGroups: Array<[string, SessionMeta[]]>;
  /** 渲染期过滤影子/团队空间会话（hiddenIds 每次渲染都是新 Set，不能进 effect 依赖）。 */
  hiddenIds: Set<string>;
  open: boolean;
  onToggle: () => void;
  /** 检索关键词（状态由 AppShell 持有：批量栏/删除确认也会清空它）。 */
  query: string;
  onQueryChange: (value: string) => void;
  /** 命中行打开语义：标题命中 = 普通打开；消息命中 = 记锚点再打开。 */
  onOpenFromSearch: (sid: string, messageId?: string) => void;
  /** 清理已归档：删除当前列表里的全部已归档会话（确认弹窗在 AppShell）。 */
  onClearArchived: () => void;
  /** 批量动作进行中：清理按钮禁用。 */
  bulkBusy: boolean;
  renderSessionRow: (
    s: SessionMeta,
    depth?: number,
    childRows?: SessionMeta[],
    dimmed?: boolean,
  ) => React.ReactNode;
}

interface ArchivedSearchResult {
  sessions: SessionMeta[];
  messages: SearchMessageHit[];
  indexing: boolean;
  indexed: number;
}

/**
 * 已归档（P1-1）：默认收起，标题带计数；行压暗但仍可点开/恢复。
 * 两种视图下都固定挂在侧栏最底部。P0-2：展开后带本地检索框（标题 + 消息
 * 片段），有搜索时用结果替代普通列表；无搜索时按 YYYY-MM 分月、最新在前。
 * 展开时标题行右侧有「清理已归档」垃圾桶按钮（确认弹窗与执行在 AppShell，
 * 走既有 bulk delete：被引用会话由后端跳过并列在提示条）。
 * 检索的 200ms 防抖与结果状态随 UI 一起收在这里；关键词本身仍由 AppShell
 * 持有（批量栏「恢复所选」和删除确认都会清空它）。
 */
export function SidebarArchive({
  archivedSessions,
  archivedMonthGroups,
  hiddenIds,
  open,
  onToggle,
  query,
  onQueryChange,
  onOpenFromSearch,
  onClearArchived,
  bulkBusy,
  renderSessionRow,
}: SidebarArchiveProps) {
  const t = useTranslations("shell");
  const [search, setSearch] = useState<ArchivedSearchResult | null>(null);
  const [loading, setLoading] = useState(false);
  const needle = query.trim();

  // 归档检索（P0-2）：200ms 防抖 → /api/search?scope=archived。清空关键词
  // 立即回到按月份分组的普通归档列表。
  useEffect(() => {
    if (!open || !needle) {
      setSearch(null);
      setLoading(false);
      return;
    }
    let alive = true;
    setSearch(null);
    setLoading(true);
    const timer = setTimeout(() => {
      api
        .searchAll(needle, { scope: "archived", limit: 30 })
        .then((r) => {
          if (!alive) return;
          setSearch({
            sessions: r.sessions ?? [],
            messages: r.messages ?? [],
            indexing: !!r.index?.indexing,
            indexed: r.index?.indexed ?? 0,
          });
        })
        .catch(() => {
          if (alive) {
            setSearch({ sessions: [], messages: [], indexing: false, indexed: 0 });
          }
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
    }, 200);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
    // hiddenIds 每次渲染都是新 Set，不能进依赖（会让防抖永不稳定）；过滤放在
    // 渲染期做（见 titleHits / messageHits）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, needle]);

  const titleHits = (search?.sessions ?? []).filter((s) => !hiddenIds.has(s.id));
  const messageHits = (search?.messages ?? []).filter((m) => !hiddenIds.has(m.session_id));

  if (archivedSessions.length === 0) return null;

  return (
    <div className="mb-1">
      {/* 标题行：折叠开关占满整行，展开时右侧挂「清理已归档」——按钮不能嵌套
          在 button 里，所以外层是 div、开关自己 flex-1。 */}
      <div className="flex items-center gap-1 pb-1 pt-3">
        <button
          onClick={onToggle}
          title={t("groups.archivedHint")}
          className="flex flex-1 items-center gap-1 px-2.5 text-left text-[11px] font-medium text-faint transition-colors hover:text-muted"
        >
          <ChevronDown
            className={`h-3 w-3 shrink-0 transition-transform ${
              open ? "" : "-rotate-90"
            }`}
          />
          {t("groups.archived", { count: archivedSessions.length })}
        </button>
        {open && (
          <button
            onClick={onClearArchived}
            disabled={bulkBusy}
            aria-label={t("bulk.clearArchived")}
            title={t("bulk.clearArchivedHint", { count: archivedSessions.length })}
            className="mr-1 rounded p-1 text-faint transition-colors hover:bg-card2 hover:text-red disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        )}
      </div>
      {open && (
        <div className="space-y-0.5">
          <div className="px-1.5 pb-1">
            <div className="flex items-center gap-1 rounded-md border border-line bg-base/40 px-1.5">
              <Search className="h-3 w-3 shrink-0 text-faint" />
              <input
                value={query}
                onChange={(e) => onQueryChange(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    e.preventDefault();
                    onQueryChange("");
                  }
                }}
                placeholder={t("archive.searchPlaceholder")}
                aria-label={t("archive.searchLabel")}
                className="w-full bg-transparent py-1 text-[11px] text-txt outline-none placeholder:text-faint"
              />
              {query && (
                <button
                  onClick={() => onQueryChange("")}
                  aria-label={t("archive.clearSearch")}
                  title={t("archive.clearSearch")}
                  className="shrink-0 text-faint hover:text-txt"
                >
                  ✕
                </button>
              )}
            </div>
          </div>
          {needle ? (
            <>
              {loading && !search && (
                <div className="px-2.5 py-1 text-[10px] text-faint">
                  {t("search.loading")}
                </div>
              )}
              {titleHits.length > 0 && (
                <div className="px-2.5 pb-0.5 pt-0.5 text-[10px] font-medium text-faint">
                  {t("search.sections.sessions")}
                </div>
              )}
              {titleHits.map((s) => (
                <SessionHitRow
                  key={s.id}
                  session={s}
                  dense
                  onOpen={() => onOpenFromSearch(s.id)}
                />
              ))}
              {messageHits.length > 0 && (
                <div className="px-2.5 pb-0.5 pt-1.5 text-[10px] font-medium text-faint">
                  {t("search.sections.messages")}
                </div>
              )}
              {messageHits.map((hit, i) => (
                <MessageHitRow
                  key={`${hit.session_id}:${hit.message_id}:${i}`}
                  hit={hit}
                  needle={needle}
                  dense
                  onOpen={() => onOpenFromSearch(hit.session_id, hit.message_id)}
                />
              ))}
              {search && search.indexing && messageHits.length === 0 && (
                <div className="px-2.5 py-1 text-[10px] text-faint">
                  {t("search.indexing")}
                </div>
              )}
              {!loading && search && titleHits.length === 0 && messageHits.length === 0 && (
                <div className="px-2.5 py-1 text-[10px] text-faint">
                  {t("archive.noResults")}
                </div>
              )}
            </>
          ) : (
            archivedMonthGroups.map(([month, rows]) => (
              <div key={month}>
                <div className="sticky top-0 z-10 bg-panel/95 px-2.5 pb-0.5 pt-1.5 text-[10px] font-medium text-faint backdrop-blur">
                  {month}
                </div>
                {rows.map((s) =>
                  renderSessionRow(s, 0, undefined, true),
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
