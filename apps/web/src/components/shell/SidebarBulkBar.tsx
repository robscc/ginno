"use client";

import { useTranslations } from "next-intl";

export interface BulkSkippedItem {
  id: string;
  title: string;
  references?: string[];
}

export interface SidebarBulkBarProps {
  selectMode: boolean;
  /** 当前勾选总数。 */
  selectedCount: number;
  /** 勾选里落在归档子集的条数（P0-2 快捷动作只作用于这些）。 */
  selectedArchivedCount: number;
  bulkBusy: boolean;
  /** 批量删除被后端按引用跳过（skipped）的会话，动作结束后仍留在侧栏提示。 */
  bulkNotice: BulkSkippedItem[];
  bulkError: string | null;
  onDismissNotice: () => void;
  onDismissError: () => void;
  onExitSelect: () => void;
  onArchive: () => void;
  onUnarchive: () => void;
  onRequestDelete: () => void;
  /** 恢复所选：清空归档检索后对归档子集执行 unarchive。 */
  onRestoreArchived: () => void;
  onRequestArchivedDelete: () => void;
}

/**
 * 侧栏多选（P1-4）：贴底部的操作条 + 批量动作的提示条（被引用跳过 / 失败）。
 * 提示条在非多选态也会显示（动作结束后用户可能已退出多选）。
 */
export function SidebarBulkBar({
  selectMode,
  selectedCount,
  selectedArchivedCount,
  bulkBusy,
  bulkNotice,
  bulkError,
  onDismissNotice,
  onDismissError,
  onExitSelect,
  onArchive,
  onUnarchive,
  onRequestDelete,
  onRestoreArchived,
  onRequestArchivedDelete,
}: SidebarBulkBarProps) {
  const t = useTranslations("shell");
  return (
    <>
      {/* 批量删除被引用拒绝的会话（P1-4）：后端按引用跳过，这里列出标题 +
          前 2 个引用来源，便于用户先去解除引用。 */}
      {bulkNotice.length > 0 && (
        <div className="mx-2 mb-2 rounded-md border border-yellow/40 bg-yellow/10 px-2 py-1.5 text-[11px] leading-snug text-yellow">
          <div className="flex items-start gap-1">
            <span className="flex-1">{t("bulk.skipped", { count: bulkNotice.length })}</span>
            <button
              onClick={onDismissNotice}
              aria-label={t("bulk.dismiss")}
              title={t("bulk.dismiss")}
              className="shrink-0 text-yellow/70 hover:text-yellow"
            >
              ✕
            </button>
          </div>
          <ul className="mt-1 space-y-0.5">
            {bulkNotice.map((it) => (
              <li
                key={it.id}
                className="truncate"
                title={`${it.id}${it.references?.length ? ` · ${it.references.join(", ")}` : ""}`}
              >
                {it.title}
                {it.references?.length
                  ? ` · ${it.references.slice(0, 2).join(", ")}`
                  : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      {bulkError && (
        <div className="mx-2 mb-2 flex items-start gap-1 rounded-md border border-red/40 bg-red/10 px-2 py-1.5 text-[11px] text-red">
          <span className="selectable flex-1">{bulkError}</span>
          <button
            onClick={onDismissError}
            aria-label={t("bulk.dismiss")}
            className="shrink-0 text-red/70 hover:text-red"
          >
            ✕
          </button>
        </div>
      )}

      {/* 多选操作条（P1-4）：贴侧栏底部，计数 + 归档/取消归档/删除/取消 */}
      {selectMode && (
        <div className="border-t border-line bg-panel px-2.5 py-2">
          <div className="mb-1.5 flex items-center justify-between text-[11px]">
            <span className="text-muted">{t("bulk.selected", { count: selectedCount })}</span>
            <button onClick={onExitSelect} className="text-faint hover:text-txt">
              {t("bulk.cancel")}
            </button>
          </div>
          {/* P0-2：选中项里有已归档会话时，提供只作用于归档子集的快捷动作。 */}
          {selectedArchivedCount > 0 && (
            <div className="mb-1.5 flex items-center gap-1">
              <button
                onClick={onRestoreArchived}
                disabled={bulkBusy}
                className="flex-1 rounded-md border border-violet/40 px-2 py-1 text-[11px] text-violet transition-colors hover:bg-violet/10 disabled:cursor-default disabled:opacity-40"
              >
                {t("bulk.restoreSelected", { count: selectedArchivedCount })}
              </button>
              <button
                onClick={onRequestArchivedDelete}
                disabled={bulkBusy}
                className="flex-1 rounded-md border border-red/40 px-2 py-1 text-[11px] text-red transition-colors hover:bg-red/10 disabled:cursor-default disabled:opacity-40"
              >
                {t("bulk.deleteSelected", { count: selectedArchivedCount })}
              </button>
            </div>
          )}
          <div className="flex items-center gap-1">
            <button
              onClick={onArchive}
              disabled={!selectedCount || bulkBusy}
              className="flex-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:text-txt disabled:cursor-default disabled:opacity-40"
            >
              {t("bulk.archive")}
            </button>
            <button
              onClick={onUnarchive}
              disabled={!selectedCount || bulkBusy}
              className="flex-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:text-txt disabled:cursor-default disabled:opacity-40"
            >
              {t("bulk.unarchive")}
            </button>
            <button
              onClick={onRequestDelete}
              disabled={!selectedCount || bulkBusy}
              className="flex-1 rounded-md border border-red/40 px-2 py-1 text-[11px] text-red transition-colors hover:bg-red/10 disabled:cursor-default disabled:opacity-40"
            >
              {t("bulk.delete")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}
