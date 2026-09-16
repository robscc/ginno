"use client";

import { useState } from "react";
import { Loader2, Pencil, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { MemoryDraft } from "@/lib/types";
import { DiffView } from "@/components/workflow/DiffView";

/** Gate 2 review modal: distillation produces a DRAFT; MEMORY.md is only
 * written after the user reviews the diff (agent drafts, human approves).
 * The draft text is editable before applying; over-budget and
 * concurrently-changed-memory conditions surface as explicit warnings. */
export function MemoryDraftModal({
  draft,
  onResolved,
  onClose,
}: {
  draft: MemoryDraft;
  onResolved: () => void; // apply/discard done — panel reloads badge + content
  onClose: () => void;
}) {
  const [text, setText] = useState(draft.draft ?? "");
  const [editing, setEditing] = useState(false);
  const [showDiff, setShowDiff] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [armForce, setArmForce] = useState(false);

  async function apply() {
    setBusy(true);
    setMsg("");
    try {
      const force = armForce;
      const r = await api.applyMemoryDraft(editing ? text : undefined, force);
      if (r.ok) {
        onResolved();
        onClose();
        return;
      }
      if (r.error === "memory_changed") {
        setMsg("MEMORY.md 在草稿生成后已发生变化。确认无误可强制采纳（将覆盖当前记忆）。");
        setArmForce(true);
      } else {
        setMsg(r.error || "采纳失败");
      }
    } catch {
      setMsg("采纳失败：无法连接运行时");
    } finally {
      setBusy(false);
    }
  }

  async function discard() {
    setBusy(true);
    setMsg("");
    try {
      await api.discardMemoryDraft();
      onResolved();
      onClose();
    } catch {
      setMsg("丢弃失败：无法连接运行时");
      setBusy(false);
    }
  }

  const chars = text.length;
  const budget = draft.budget ?? 3000;
  const over = chars > budget;
  const created = draft.created_at ? new Date(draft.created_at * 1000).toLocaleString() : "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl border border-line2 bg-card shadow-2xl"
      >
        <div className="flex items-center border-b border-line px-4 py-3">
          <span className="rounded-full bg-violet/20 px-2 py-0.5 text-[11px] text-violet">
            记忆草稿
          </span>
          <span className="ml-2 text-xs text-muted">
            来自 {draft.pool_entries ?? 0} 条对话
            {draft.trigger === "auto" ? " · 阈值自动触发" : " · 手动触发"}
            {created ? ` · ${created}` : ""}
          </span>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="ml-auto rounded-lg p-1 text-muted hover:bg-card2 hover:text-txt"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <div className="mb-2 flex items-center gap-3 text-[11px] text-muted">
            <span className={over ? "font-medium text-red" : ""}>
              {chars}/{budget} 字{over ? `（超出预算 ${chars - budget} 字）` : ""}
            </span>
            <button
              onClick={() => setShowDiff((v) => !v)}
              className="text-violet hover:opacity-80"
            >
              {showDiff ? "收起差异" : "查看差异"}
            </button>
            <button
              onClick={() => setEditing((v) => !v)}
              className="flex items-center gap-1 text-violet hover:opacity-80"
            >
              <Pencil className="h-3 w-3" />
              {editing ? "停止编辑" : "编辑草稿"}
            </button>
          </div>
          {showDiff && !editing && (
            <div className="mb-3">
              <DiffView diff={draft.diff ?? ""} />
            </div>
          )}
          {editing ? (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              spellCheck={false}
              className="h-64 w-full resize-y rounded-lg border border-line bg-base/40 p-3 font-mono text-xs leading-relaxed text-txt focus:border-violet focus:outline-none"
            />
          ) : (
            <pre className="whitespace-pre-wrap rounded-lg border border-line bg-base/40 p-3 text-xs leading-relaxed text-muted">
              {draft.draft}
            </pre>
          )}
          {msg && <div className="mt-2 text-xs text-yellow">{msg}</div>}
        </div>

        <div className="flex items-center gap-2 border-t border-line px-4 py-3">
          <span className="text-[11px] text-faint">采纳后写入 MEMORY.md 并清空已蒸馏的对话池</span>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={discard}
              disabled={busy}
              className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-red disabled:opacity-50"
            >
              丢弃
            </button>
            <button
              onClick={apply}
              disabled={busy}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {busy && <Loader2 className="h-3 w-3 animate-spin" />}
              {armForce ? "确认强制采纳？" : "采纳并更新记忆"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
