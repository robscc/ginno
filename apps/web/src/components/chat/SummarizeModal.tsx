"use client";

import { Loader2, Workflow, X } from "lucide-react";

/**
 * 聊天页「总结成流程」确认弹层 (design A a2): shows the DSL draft distilled from
 * the current session; user can 创建并运行 / 仅创建 / 取消. On failure the modal
 * STAYS open and shows the reason inline (the draft must not be lost silently).
 */
export function SummarizeModal({
  dsl,
  busy,
  error,
  onClose,
  onCreate,
}: {
  dsl: Record<string, unknown>;
  busy?: "create" | "run" | null;
  error?: string | null;
  onClose: () => void;
  onCreate: (run: boolean) => void;
}) {
  const name = (dsl.name as string) || "新流程";
  const nodes = (dsl.nodes as Array<Record<string, unknown>>) || [];
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose} role="dialog">
      <div
        className="w-full max-w-2xl overflow-hidden rounded-xl border border-line2 bg-card shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <Workflow className="h-4 w-4 text-violet" />
          <span className="text-sm font-semibold text-txt">总结成流程 · {name}</span>
          <span className="ml-auto text-xs text-faint">summarize-from-session · 草稿未保存</span>
          <button onClick={onClose} className="rounded p-1 text-faint hover:bg-card2 hover:text-txt" aria-label="关闭">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="max-h-[55vh] overflow-auto p-4">
          <div className="mb-2 text-xs text-muted">
            从当前会话提炼出 <span className="text-violet">{nodes.length}</span> 个节点（含 loop/branch 识别）。确认后才创建为 v1：
          </div>
          <pre className="whitespace-pre-wrap rounded-lg border border-line bg-base p-3 font-mono text-[11px] leading-relaxed text-violet/90">
            {JSON.stringify(dsl, null, 2)}
          </pre>
        </div>
        {error && (
          <div className="mx-4 mb-2 rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-xs text-red">
            {error}
          </div>
        )}
        <div className="flex justify-end gap-2 border-t border-line px-4 py-3">
          <button onClick={onClose} className="btn-press rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2">
            取消
          </button>
          <button
            disabled={!!busy}
            onClick={() => onCreate(false)}
            className="btn-press flex items-center gap-1 rounded-md border border-line px-3 py-1.5 text-xs text-txt hover:bg-card2 disabled:opacity-50"
          >
            {busy === "create" && <Loader2 className="h-3 w-3 animate-spin" />}
            {busy === "create" ? "创建中…" : "仅创建"}
          </button>
          <button
            disabled={!!busy}
            onClick={() => onCreate(true)}
            className="btn-press flex items-center gap-1 rounded-md bg-gradient-to-r from-violet to-fuchsia px-3 py-1.5 text-xs font-semibold text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === "run" && <Loader2 className="h-3 w-3 animate-spin" />}
            {busy === "run" ? "运行中…" : "创建并运行"}
          </button>
        </div>
      </div>
    </div>
  );
}
