"use client";

import { useTranslations } from "next-intl";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { SessionMeta } from "@/lib/types";

export interface SidebarConfirmModalsProps {
  archiveAllConfirm: boolean;
  archiveCandidateCount: number;
  onCancelArchiveAll: () => void;
  onConfirmArchiveAll: () => void;
  bulkDeleteConfirm: boolean;
  selectedCount: number;
  onCancelBulkDelete: () => void;
  onConfirmBulkDelete: () => void;
  archivedDeleteConfirm: boolean;
  selectedArchivedCount: number;
  onCancelArchivedDelete: () => void;
  onConfirmArchivedDelete: () => void;
  deleteTarget: SessionMeta | null;
  descendantCount: (id: string) => number;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
}

/**
 * 侧栏的四个破坏性动作确认：一键归档、批量删除、归档区「删除所选」、
 * 单条删除（含级联后代提示）。状态与动作仍由 AppShell 持有。
 */
export function SidebarConfirmModals({
  archiveAllConfirm,
  archiveCandidateCount,
  onCancelArchiveAll,
  onConfirmArchiveAll,
  bulkDeleteConfirm,
  selectedCount,
  onCancelBulkDelete,
  onConfirmBulkDelete,
  archivedDeleteConfirm,
  selectedArchivedCount,
  onCancelArchivedDelete,
  onConfirmArchivedDelete,
  deleteTarget,
  descendantCount,
  onCancelDelete,
  onConfirmDelete,
}: SidebarConfirmModalsProps) {
  const t = useTranslations("shell");
  return (
    <>
      {/* P1-1 一键归档确认：候选 = Earlier 里 >14 天无活动且未归档的非置顶会话 */}
      {archiveAllConfirm && (
        <ConfirmModal
          title={t("bulk.confirmArchiveTitle")}
          message={t("bulk.confirmArchiveMessage", { count: archiveCandidateCount })}
          confirmLabel={t("bulk.confirmArchiveConfirm")}
          onConfirm={onConfirmArchiveAll}
          onCancel={onCancelArchiveAll}
        />
      )}

      {/* P1-4 批量删除确认：后端会跳过仍被引用的会话并在侧栏提示 */}
      {bulkDeleteConfirm && (
        <ConfirmModal
          title={t("bulk.confirmDeleteTitle")}
          message={t("bulk.confirmDeleteMessage", { count: selectedCount })}
          confirmLabel={t("bulk.confirmDeleteConfirm")}
          onConfirm={onConfirmBulkDelete}
          onCancel={onCancelBulkDelete}
        />
      )}

      {/* P0-2 归档区「删除所选」：只删选中的已归档子集，确认后清空检索。 */}
      {archivedDeleteConfirm && (
        <ConfirmModal
          title={t("bulk.confirmDeleteTitle")}
          message={t("bulk.confirmDeleteMessage", { count: selectedArchivedCount })}
          confirmLabel={t("bulk.confirmDeleteConfirm")}
          onConfirm={onConfirmArchivedDelete}
          onCancel={onCancelArchivedDelete}
        />
      )}

      {deleteTarget && (
        <ConfirmModal
          title={t("deleteModal.title")}
          message={`${t("deleteModal.message", {
            name: deleteTarget.title || t("session.untitled"),
          })}${
            descendantCount(deleteTarget.id) > 0
              ? ` ${t("deleteModal.cascade", { count: descendantCount(deleteTarget.id) })}`
              : ""
          }`}
          confirmLabel={t("deleteModal.confirm")}
          onConfirm={onConfirmDelete}
          onCancel={onCancelDelete}
        />
      )}
    </>
  );
}
