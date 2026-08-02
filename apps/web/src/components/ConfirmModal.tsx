"use client";

/** In-app confirmation modal. Used instead of window.confirm because the native
 *  dialog is unreliable in the Tauri webview; being React-rendered, this works
 *  identically in Tauri and the browser. */
export function ConfirmModal({
  title,
  message,
  confirmLabel = "删除",
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
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
            取消
          </button>
          <button
            onClick={onConfirm}
            className="rounded-lg bg-red px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
