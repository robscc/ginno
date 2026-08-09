"use client";

import { useState } from "react";
import { Check, Loader2, MessageSquare, SkipForward, Square } from "lucide-react";
import { cancelWorkflowRun, resumeWorkflowRun } from "@/lib/runtime";
import { Markdown } from "@/components/chat/Markdown";

/**
 * In-run answer card for a paused human node (workflow-ux-redesign P1).
 * Renders inside LiveRunBlock when `run.status === "paused"` and the run's
 * `pending_interrupt.kind === "human"`: shows the node's question, an optional
 * free-text reply, and 确认继续 / 跳过 / 中止运行.
 */
export function HumanInputCard({
  runId,
  question,
  nodeTitle,
}: {
  runId: string;
  question: string | null;
  nodeTitle?: string;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState<null | "answer" | "skip" | "cancel">(null);
  const [done, setDone] = useState<null | string>(null); // submission receipt text

  const submit = async (kind: "answer" | "skip") => {
    if (busy) return;
    setBusy(kind);
    try {
      await resumeWorkflowRun(runId, kind === "answer" ? { answer } : { answer: null, skip: true });
      setDone(kind === "answer" ? (answer.trim() ? `已回复：${answer.trim()}` : "已确认继续") : "已跳过");
    } catch {
      setBusy(null);
    }
  };

  const cancel = async () => {
    if (busy) return;
    setBusy("cancel");
    try {
      await cancelWorkflowRun(runId);
    } catch {
      setBusy(null);
    }
  };

  if (done) {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-md border border-line bg-card2/40 px-2.5 py-1.5 text-[11px] text-muted">
        <Check className="h-3 w-3 text-green" /> {done}
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-md border-2 border-yellow/40 bg-yellow/[0.05] p-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-yellow">
        <MessageSquare className="h-3.5 w-3.5" />
        需要你的输入{nodeTitle ? <span className="font-normal text-faint">· {nodeTitle}</span> : null}
      </div>
      {question && (
        <div className="mb-2 text-xs leading-relaxed text-txt [&_p]:my-1">
          <Markdown text={question} />
        </div>
      )}
      <textarea
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
            e.preventDefault();
            void submit("answer");
          }
        }}
        placeholder="回复（可选，⌘/Ctrl+Enter 提交）"
        rows={2}
        className="mb-2 w-full resize-none rounded border border-line bg-card px-2 py-1.5 text-xs text-txt placeholder:text-faint focus:border-violet/60 focus:outline-none"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          onClick={() => void submit("answer")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-md bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy === "answer" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
          确认继续
        </button>
        <button
          onClick={() => void submit("skip")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-card2 hover:text-txt disabled:opacity-50"
        >
          {busy === "skip" ? <Loader2 className="h-3 w-3 animate-spin" /> : <SkipForward className="h-3 w-3" />}
          跳过
        </button>
        <button
          onClick={() => void cancel()}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
        >
          {busy === "cancel" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3" />}
          中止运行
        </button>
      </div>
    </div>
  );
}
