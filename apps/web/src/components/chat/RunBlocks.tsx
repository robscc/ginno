"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, Check, Circle, Loader2, MessageSquare, RotateCcw, SkipForward, Square, Trash2, Workflow, Wrench } from "lucide-react";
import type { WorkflowRun } from "@/lib/types";
import { useGinno } from "@/lib/store";
import { RunErrorBox } from "@/components/workflow/RunErrorBox";
import { HumanInputCard } from "@/components/workflow/HumanInputCard";

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  paused: "#f59e0b",
  pending: "#71717a",
  failed: "#ef4444",
  cancelled: "#71717a",
  interrupted: "#f97316",
  skipped: "#a1a1aa",
};

export const STATUS_LABEL: Record<string, string> = {
  done: "已完成",
  ok: "已完成",
  running: "运行中",
  paused: "已暂停",
  pending: "待执行",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  skipped: "已跳过",
};

function Glyph({ status }: { status?: string }) {
  const c = STATUS_COLOR[status || "pending"] || STATUS_COLOR.pending;
  if (status === "done" || status === "ok") return <Check className="h-3.5 w-3.5" style={{ color: c }} />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: c }} />;
  if (status === "skipped") return <SkipForward className="h-3.5 w-3.5" style={{ color: c }} />;
  return <Circle className="h-3.5 w-3.5" style={{ color: c }} />;
}

// Paused-at-human-step glyph (workflow-ux-redesign P1): a waiting step is not
// "spinning work" — it's waiting for YOU. Pulsing yellow speech bubble.
function HumanWaitGlyph() {
  return <MessageSquare className="h-3.5 w-3.5 animate-pulse text-yellow" />;
}

function fmtElapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);
const RETRYABLE = new Set(["failed", "cancelled", "interrupted"]);
const STUCK_FALLBACK_S = 120; // no history yet → 2 min (was a fixed 5 min)

/**
 * In-chat live run block (design A): renders a workflow run bound to this session,
 * driven by run.event/run.status push events. Shows live step states + controls
 * (cancel while running, continue when paused, retry/delete when terminal) + a
 * link to the workflow page. Shared by the right-panel Workflow tab.
 */
export function LiveRunBlock({
  run,
  onCancel,
  onContinue,
  onRetry,
  onDelete,
  onRetryFromCheckpoint,
}: {
  run: WorkflowRun;
  onCancel?: (runId: string) => void;
  onContinue?: (runId: string) => void;
  onRetry?: (runId: string) => void | Promise<{ ok?: boolean; detail?: string } | void>;
  onDelete?: (runId: string) => void;
  onRetryFromCheckpoint?: (runId: string) => Promise<{ ok?: boolean; detail?: string } | void>;
}) {
  const router = useRouter();
  const g = useGinno();
  const done = run.steps.filter((s) => s.status === "done").length;
  const total = run.steps.length;
  const c = STATUS_COLOR[run.status] || STATUS_COLOR.pending;
  const label = STATUS_LABEL[run.status] || run.status;
  const isTerminal = TERMINAL.has(run.status);
  const showFailure = run.status === "failed" || run.status === "interrupted" || run.status === "cancelled";
  // Paused because a human node asked a question → answer card (P1). Other
  // paused reasons (supervisor) keep the generic 继续 button.
  const humanInterrupt =
    run.status === "paused" && run.pending_interrupt?.kind === "human"
      ? run.pending_interrupt
      : null;
  // Live in-flight tool call for the current step (P1 visibility).
  const activity = run.status === "running" ? g.liveToolActivity[run.id] : undefined;

  // Entrance reaction (work item D): runs born after this component mounted
  // (UI trigger / agent run.bind / retry product) slide in + pulse twice.
  // Historical rows loaded on first paint stay quiet.
  const mountedAt = useRef(Date.now());
  const isNew = run.started * 1000 > mountedAt.current - 2000;

  // Retry reaction: busy spinner, then either the "已重试" hand-off (data
  // refresh stamps retry_run_id) or a shake + inline reason.
  const [retryBusy, setRetryBusy] = useState(false);
  const [retryErr, setRetryErr] = useState<string | null>(null);
  const handleRetry = async () => {
    if (!onRetry) return;
    setRetryBusy(true);
    setRetryErr(null);
    try {
      const r = await onRetry(run.id);
      if (r && r.ok === false) {
        setRetryErr(r.detail || "重试失败");
        setRetryBusy(false);
      }
      // ok (or void): stay busy — the refresh replaces this button with 已重试
      // and the new run card animates in below/above.
    } catch {
      setRetryErr("重试失败：无法连接运行时");
      setRetryBusy(false);
    }
  };

  // Re-render once a second while the run is active so elapsed/stuck stay live.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (isTerminal) return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [isTerminal]);
  const now = Date.now() / 1000;
  const elapsed = run.status === "running" ? now - run.started : null;
  // P3 adaptive stuck: 3× the workflow's average completed duration (min 60s);
  // falls back to 2 min when there is no history yet.
  const hist = g.runDurationByWorkflow[run.workflow_id] || [];
  const avgDone = hist.length ? hist.reduce((a, b) => a + b, 0) / hist.length : null;
  const stuckAfter = avgDone ? Math.max(60, avgDone * 3) : STUCK_FALLBACK_S;
  const stuck = run.status === "running" && now - run.updated > stuckAfter;

  return (
    <div
      {...(humanInterrupt ? { "data-waiting-human": "true" } : {})}
      className={`my-2 rounded-lg border border-violet/40 bg-violet/[0.06] p-3 ${
        isNew ? "anim-slide-in anim-pulse-ring" : ""
      }`}
    >
      <div
        onClick={() => router.push("/workflows")}
        title="打开工作流详情"
        className="mb-2 flex cursor-pointer items-center gap-1.5 text-sm font-medium text-txt hover:text-violet"
      >
        {humanInterrupt ? <MessageSquare className="h-3.5 w-3.5 text-yellow" /> : <Workflow className="h-3.5 w-3.5 text-violet" />}
        {run.name || "Workflow"}
        <span className="ml-auto flex items-center gap-1.5 text-xs font-normal" style={{ color: c }}>
          {elapsed !== null && <span className="text-faint">⏱ {fmtElapsed(elapsed)}</span>}
          {stuck && (
            <span
              className="flex items-center gap-0.5 rounded-full bg-yellow/15 px-1.5 py-0.5 text-[10px] text-yellow"
              title={
                avgDone
                  ? `该流程平均 ${fmtElapsed(avgDone)} 完成，当前已 ${fmtElapsed(now - run.started)} 无进展，可取消后重试`
                  : "超过 2 分钟无进展，可取消后重试"
              }
            >
              <AlertTriangle className="h-3 w-3" /> 疑似卡住
            </span>
          )}
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
          {label} · {done}/{total}
        </span>
      </div>
      <div className="space-y-1">
        {run.steps.map((s) => {
          // The step a paused human node is waiting on (P1): speech-bubble
          // glyph instead of the running spinner.
          const waitingHuman = !!humanInterrupt && humanInterrupt.node_id === s.id;
          return (
            <div key={s.id}>
              <div className="flex items-center gap-2 text-xs">
                {waitingHuman ? <HumanWaitGlyph /> : <Glyph status={s.status} />}
                <span className={s.status === "done" ? "text-muted line-through" : "text-txt"}>{s.title}</span>
              </div>
              {/* live tool call under the running step (P1): latest in-flight
                  call only; cleared when its tool_result lands. */}
              {activity && activity.nodeId === s.id && s.status === "running" && (
                <div className="ml-5 flex items-center gap-1.5 text-[11px] text-faint">
                  <Wrench className="h-3 w-3 shrink-0" />
                  <span className="font-mono text-muted">{activity.toolName}</span>
                  {activity.argsPreview && <span className="truncate">· {activity.argsPreview}</span>}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {showFailure && (
        <RunErrorBox
          run={run}
          onRetryFromCheckpoint={onRetryFromCheckpoint ? () => onRetryFromCheckpoint(run.id) : undefined}
        />
      )}

      {/* Human answer card (P1): replaces the generic 继续 button when the run
          is paused at a human node with a question to answer. */}
      {humanInterrupt && (
        <HumanInputCard
          runId={run.id}
          question={(humanInterrupt.question as string | undefined) ?? null}
          nodeTitle={run.steps.find((s) => s.id === humanInterrupt.node_id)?.title}
        />
      )}

      {(run.status === "running" || (run.status === "paused" && !humanInterrupt)) && (
        <div className="mt-2 flex gap-2">
          {run.status === "running" && onCancel && (
            <button
              onClick={() => onCancel(run.id)}
              className="btn-press flex items-center gap-1 rounded-md border border-red/40 px-2 py-1 text-xs text-red hover:bg-red/10"
            >
              <Square className="h-3 w-3" /> 取消
            </button>
          )}
          {run.status === "paused" && onContinue && (
            <button
              onClick={() => onContinue(run.id)}
              className="btn-press flex items-center gap-1 rounded-md border border-yellow/40 px-2 py-1 text-xs text-yellow hover:bg-yellow/10"
            >
              <Check className="h-3 w-3" /> 继续（human/supervisor）
            </button>
          )}
        </div>
      )}

      {isTerminal && RETRYABLE.has(run.status) && (onRetry || onDelete) && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {onRetry &&
            (run.retry_run_id ? (
              <span className="flex items-center gap-1 text-[11px] text-faint">已重试</span>
            ) : (
              <button
                onClick={handleRetry}
                disabled={retryBusy}
                className={`btn-press flex items-center gap-1 rounded-md border border-violet/40 px-2 py-1 text-xs text-violet hover:bg-violet/10 disabled:opacity-50 ${
                  retryErr ? "anim-shake" : ""
                }`}
              >
                {retryBusy ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <RotateCcw className="h-3 w-3" />
                )}
                {retryBusy ? "重试中…" : "重试"}
              </button>
            ))}
          {onDelete && (
            <button
              onClick={() => onDelete(run.id)}
              className="btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-red/10 hover:text-red"
            >
              <Trash2 className="h-3 w-3" /> 删除
            </button>
          )}
          {retryErr && <span className="text-[11px] text-red">{retryErr}</span>}
        </div>
      )}
    </div>
  );
}
