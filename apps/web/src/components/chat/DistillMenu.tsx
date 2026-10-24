"use client";

// DistillMenu — V2 P1-2 UI entry: turn the current session into a reusable
// asset. Skill / knowledge card hit `POST /api/sessions/{id}/distill`; workflow
// reuses the existing summarize-from-session flow (SummarizeModal in
// ChatStream), since that already drafts and creates a workflow.

import { useState } from "react";
import { BookOpen, ChevronDown, Loader2, Sparkles, Workflow } from "lucide-react";
import { useTranslations } from "next-intl";

export type DistillKind = "skill" | "kb" | "workflow";

export function DistillMenu({
  busy,
  onPick,
}: {
  /** A distill/create request is in flight (button + rows disabled). */
  busy: boolean;
  onPick: (kind: DistillKind) => void;
}) {
  const tc = useTranslations("chat");
  const [open, setOpen] = useState(false);

  const pick = (kind: DistillKind) => {
    setOpen(false);
    onPick(kind);
  };

  return (
    <div className="relative shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title={tc("distill.title")}
        className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted transition-colors hover:border-line2 hover:text-txt disabled:opacity-50"
      >
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Sparkles className="h-3.5 w-3.5" />
        )}
        {busy ? tc("distill.busy") : tc("distill.button")}
        <ChevronDown className="h-3 w-3 opacity-70" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-1.5 w-64 max-w-[min(16rem,calc(100vw-2rem))] rounded-lg border border-line bg-card py-1 shadow-xl">
            <button
              onClick={() => pick("skill")}
              className="flex w-full items-start gap-2 px-3 py-1.5 text-left hover:bg-card2"
            >
              <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0 text-violet" />
              <span className="min-w-0">
                <span className="block text-xs text-txt">{tc("distill.skill")}</span>
                <span className="block truncate text-[10.5px] text-faint">{tc("distill.skillHint")}</span>
              </span>
            </button>
            <button
              onClick={() => pick("kb")}
              className="flex w-full items-start gap-2 px-3 py-1.5 text-left hover:bg-card2"
            >
              <BookOpen className="mt-0.5 h-3.5 w-3.5 shrink-0 text-green" />
              <span className="min-w-0">
                <span className="block text-xs text-txt">{tc("distill.kb")}</span>
                <span className="block truncate text-[10.5px] text-faint">{tc("distill.kbHint")}</span>
              </span>
            </button>
            <button
              onClick={() => pick("workflow")}
              className="flex w-full items-start gap-2 px-3 py-1.5 text-left hover:bg-card2"
            >
              <Workflow className="mt-0.5 h-3.5 w-3.5 shrink-0 text-orange" />
              <span className="min-w-0">
                <span className="block text-xs text-txt">{tc("distill.workflow")}</span>
                <span className="block truncate text-[10.5px] text-faint">{tc("distill.workflowHint")}</span>
              </span>
            </button>
          </div>
        </>
      )}
    </div>
  );
}
