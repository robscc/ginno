"use client";

import { useTranslations } from "next-intl";

/** In-app confirmation modal. Used instead of window.confirm because the native
 *  dialog is unreliable in the Tauri webview; being React-rendered, this works
 *  identically in Tauri and the browser. */
export function ConfirmModal({
  title,
  message,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations("ui");
  // 未显式传入确认键时默认「删除」（危险操作），取消键走 ui 域通用文案
  const confirmText = confirmLabel ?? t("common.delete");
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={onCancel}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-line bg-card p-4 shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="text-sm font-semibold text-txt">{title}</div>
        <div className="mt-2 text-xs leading-relaxed text-muted">{message}</div>
        <div className="mt-4 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
          >
            {t("common.cancel")}
          </button>
          <button
            onClick={onConfirm}
            className="rounded-lg bg-red px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
