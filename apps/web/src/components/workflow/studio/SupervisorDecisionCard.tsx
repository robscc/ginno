"use client";

import { useState } from "react";
import { AlertTriangle, Check, ChevronDown, Loader2, RotateCcw, SkipForward, Square, Pencil } from "lucide-react";
import * as api from "@/lib/runtime";
import { FALLBACK_REASON_ZH } from "./SupervisorConsole";

/**
 * Decision card for a run parked at an injected supervisor gate
 * (pending_interrupt.kind === "supervisor", design B §8.5). Six decisions:
 * 继续 / 重试该节点 / 跳过 / 改 context 后继续 / 保持暂停 / 中止.
 * All land on POST /decide {decision, context_patch}.
 *
 * When the parked interrupt carries fallback_reason/auto_suggestion the auto
 * adjudicator escalated this gate to a human (P2.5) — shown as a banner above
 * the question; the six decisions themselves are unchanged.
 */
export function SupervisorDecisionCard({
  runId,
  nodeId,
  question,
  fallbackReason,
  autoSuggestion,
  onChanged,
}: {
  runId: string;
  nodeId?: string | null;
  question?: string | null;
  fallbackReason?: string | null;
  /** The backend sends the adjudicator's FULL verdict object
   *  {decision, confidence, reason}; tolerate a plain string too. */
  autoSuggestion?: string | { decision?: string; confidence?: number; reason?: string } | null;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [patchOpen, setPatchOpen] = useState(false);
  const [patchText, setPatchText] = useState("{\n  \"\": \"\"\n}");
  const [collapsed, setCollapsed] = useState(false);

  const decide = async (decision: string, patch?: Record<string, unknown>) => {
    if (busy) return;
    setBusy(decision);
    setErr(null);
    try {
      await api.decideWorkflowRun(runId, decision, patch);
      onChanged();
    } catch {
      setErr("裁决提交失败（运行时未响应）");
    } finally {
      setBusy(null);
    }
  };

  const applyPatch = () => {
    let patch: Record<string, unknown> | undefined;
    if (patchText.trim()) {
      try {
        patch = JSON.parse(patchText);
      } catch (e) {
        setErr(`context_patch 不是合法 JSON：${(e as Error).message}`);
        return;
      }
    }
    void decide("continue", patch);
  };

  if (collapsed) {
    return (
      <button
        onClick={() => setCollapsed(false)}
        className="flex w-full items-center gap-1.5 rounded-md border border-yellow/40 bg-yellow/[0.05] px-2.5 py-1.5 text-left text-[11px] text-yellow hover:bg-yellow/[0.1]"
      >
        <AlertTriangle className="h-3 w-3 shrink-0" />
        运行在此处保持暂停（{nodeId || "检查点"}）— 点开裁决
        <ChevronDown className="ml-auto h-3 w-3" />
      </button>
    );
  }

  return (
    <div className="rounded-md border-2 border-yellow/40 bg-yellow/[0.05] p-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-yellow">
        <AlertTriangle className="h-3.5 w-3.5" />
        Supervisor 检查点
        {nodeId && <span className="font-normal text-faint">@{nodeId}</span>}
        <button
          onClick={() => setCollapsed(true)}
          className="ml-auto rounded border border-yellow/30 px-1.5 py-px text-[10px] font-normal text-yellow/80 hover:text-yellow"
        >
          保持暂停（收起）
        </button>
      </div>
      {(fallbackReason || autoSuggestion) && (
        <div className="mb-2 space-y-0.5 rounded border border-yellow/30 bg-yellow/[0.08] px-2 py-1.5">
          {fallbackReason && (
            <div className="text-[11px] text-yellow">
              auto 转人工 · 原因：{FALLBACK_REASON_ZH[fallbackReason] || fallbackReason}
            </div>
          )}
          {autoSuggestion && (
            <div className="text-[11px] text-muted">
              auto 建议：
              {typeof autoSuggestion === "string"
                ? autoSuggestion
                : `${autoSuggestion.decision ?? "?"}${
                    autoSuggestion.reason ? `（${autoSuggestion.reason}）` : ""
                  }`}
            </div>
          )}
        </div>
      )}
      {question && (
        <div className="mb-2 text-xs leading-relaxed text-txt">{question}</div>
      )}
      {patchOpen && (
        <div className="mb-2 space-y-1.5">
          <textarea
            value={patchText}
            onChange={(e) => setPatchText(e.target.value)}
            rows={3}
            spellCheck={false}
            className="w-full resize-y rounded border border-line bg-card px-2 py-1 font-mono text-[11px] text-txt outline-none focus:border-violet/60"
          />
          <div className="text-[10px] text-faint">
            合并进 context 后继续（同名键覆盖）
          </div>
        </div>
      )}
      {err && <div className="mb-2 text-[11px] text-red">{err}</div>}
      <div className="grid grid-cols-2 gap-1.5">
        {patchOpen ? (
          <button
            onClick={applyPatch}
            disabled={!!busy}
            className="btn-press flex items-center gap-1 rounded-md bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === "continue" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
            应用并继续
          </button>
        ) : (
          <button
            onClick={() => void decide("continue")}
            disabled={!!busy}
            className="btn-press flex items-center gap-1 rounded-md bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy === "continue" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
            继续
          </button>
        )}
        <button
          onClick={() => void decide("retry")}
          disabled={!!busy}
          title="回到该节点重新执行（后面的步骤随之重跑）"
          className="btn-press flex items-center gap-1 rounded-md border border-line bg-card px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          {busy === "retry" ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
          重试该节点
        </button>
        <button
          onClick={() => void decide("skip")}
          disabled={!!busy}
          title="记录一次跳过，继续走后继（该节点已执行过）"
          className="btn-press flex items-center gap-1 rounded-md border border-line bg-card px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          {busy === "skip" ? <Loader2 className="h-3 w-3 animate-spin" /> : <SkipForward className="h-3 w-3" />}
          跳过
        </button>
        <button
          onClick={() => (patchOpen ? applyPatch() : setPatchOpen(true))}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-md border border-line bg-card px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
        >
          <Pencil className="h-3 w-3" />
          改 context 后继续
        </button>
        <button
          onClick={() => void decide("abort")}
          disabled={!!busy}
          title="直接走到 END，本次运行到此为止"
          className="btn-press col-span-2 flex items-center justify-center gap-1 rounded-md border border-red/30 bg-card px-2 py-1 text-xs text-red hover:bg-red/10 disabled:opacity-50"
        >
          {busy === "abort" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3" />}
          中止
        </button>
      </div>
    </div>
  );
}