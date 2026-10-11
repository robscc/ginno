"use client";

// SummarizeMenu — composer 上方的「总结」入口下拉（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）：会话选择 + 追溯范围（最近 N 条）。

import { ChevronDown, Loader2, RotateCcw, X, Zap } from "lucide-react";
import { useTranslations } from "next-intl";
import { relTime } from "@/lib/utils";
import type { SessionMeta } from "@/lib/types";

export function SummarizeMenu({
  sessions,
  currentSessionId,
  open,
  onToggle,
  loading,
  savedDraft,
  onOpenDraftModal,
  onDeleteDraft,
  onPickSession,
  lastN,
  onLastN,
}: {
  sessions: SessionMeta[];
  currentSessionId?: string | null;
  open: boolean;
  onToggle: () => void;
  loading: boolean;
  savedDraft: { savedAt: number } | null;
  onOpenDraftModal: () => void;
  onDeleteDraft: () => void;
  onPickSession: (sid: string) => void;
  lastN: number | null;
  onLastN: (n: number | null) => void;
}) {
  const tc = useTranslations("chat");
  return (
    <div className="relative">
      <button
        onClick={onToggle}
        disabled={loading || sessions.length === 0}
        title={tc("summarize.buttonTitle")}
        className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-2.5 py-1 text-xs text-violet hover:bg-violet/20 disabled:opacity-60"
      >
        {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Zap className="h-3.5 w-3.5" />}
        {loading ? tc("summarize.busy") : tc("summarize.button")}
        <ChevronDown className="h-3 w-3 opacity-70" />
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-30 mb-1 w-64 max-w-[min(16rem,calc(100vw-2rem))] rounded-lg border border-line bg-card p-1 shadow-2xl">
          {/* S6: an unsaved draft is an OPT-IN restore, never a blocker. */}
          {savedDraft && (
            <div className="mb-1 flex items-center gap-1 rounded-md border border-violet/30 bg-violet/[0.06] px-2 py-1.5">
              <button
                onClick={onOpenDraftModal}
                title={tc("summarize.restoreDraftTitle")}
                className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-[11px] text-violet hover:opacity-80"
              >
                <RotateCcw className="h-3 w-3 shrink-0" />
                <span className="truncate">{tc("summarize.restoreDraft", { time: relTime(savedDraft.savedAt / 1000) })}</span>
              </button>
              <button
                onClick={onDeleteDraft}
                title={tc("summarize.deleteDraftTitle")}
                className="shrink-0 rounded p-0.5 text-faint hover:bg-red/10 hover:text-red"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          )}
          <div className="px-2 pb-1 pt-1.5 text-[10px] font-medium uppercase tracking-wide text-faint">
            {tc("summarize.pickSession")}
          </div>
          {sessions.slice(0, 10).map((s) => (
            <button
              key={s.id}
              onClick={() => onPickSession(s.id)}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs text-muted hover:bg-card2 hover:text-txt"
            >
              {s.id === currentSessionId && <span className="text-violet">●</span>}
              <span className="max-w-[150px] truncate">{s.title || tc("summarize.untitled")}</span>
              {s.id === currentSessionId && <span className="text-[10px] text-faint">{tc("summarize.recommended")}</span>}
              <span className="ml-auto shrink-0 text-[10px] text-faint">{relTime(s.updated)}</span>
            </button>
          ))}
          <div className="mt-1 border-t border-line2 px-2 pb-1 pt-1.5">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">{tc("summarize.range")}</div>
            <div className="flex gap-1">
              {([null, 5, 10, 20] as const).map((n) => (
                <button
                  key={String(n)}
                  onClick={() => onLastN(n)}
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    lastN === n
                      ? "bg-violet/15 text-violet"
                      : "text-faint hover:bg-card2 hover:text-muted"
                  }`}
                >
                  {n === null ? tc("summarize.all") : tc("summarize.lastN", { n })}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
