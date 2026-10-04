"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, Loader2, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { getWorkflowRunEvents } from "@/lib/runtime";
import { buildRunErrorReport, copyText, failedStep } from "@/lib/errorReport";
import { WorkflowLogTimeline } from "./WorkflowLogTimeline";

const FAILURE = new Set(["failed", "interrupted", "cancelled"]);
const TAIL_N = 15;

/**
 * Shared failure panel for a workflow run (work item C). Replaces the old
 * one-line red box: shows the one-line error + the failed step, and expands to
 * the trimmed traceback + the last events leading up to the failure. A "复制
 * 错误报告" button packages all of it as Markdown for pasting into Claude Code.
 *
 * Data: traceback comes from ``run.error_detail`` when present; otherwise (or
 * for the event tail / report) events are lazy-loaded on first expand.
 */
export function RunErrorBox({
  run,
  defaultOpen = false,
  onRetryFromCheckpoint,
}: {
  run: WorkflowRun;
  defaultOpen?: boolean;
  /** P2: 从失败步骤重试 — shown only for failed runs with node attribution
   *  where the failure is NOT the first step (nothing to skip otherwise). */
  onRetryFromCheckpoint?: () => Promise<{ ok?: boolean; detail?: string } | void>;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [events, setEvents] = useState<WorkflowRunEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeErr, setResumeErr] = useState<string | null>(null);
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // wf.error 域文案；fallbackError 原为模块级函数（直写英文），现随组件取
  // 译文案。resumeErr 默认值也走同域 key（异步回调里捕获渲染期 t，与既有
  // useChatT 模式一致）。
  const t = useTranslations("wf.error");
  const tCommon = useTranslations("wf.common");
  const fallbackError = (r: WorkflowRun): string => {
    if (r.status === "interrupted") return t("interruptedRestart");
    if (r.status === "cancelled") return t("cancelled");
    return t("failed");
  };

  const color = run.status === "interrupted" ? "#f97316" : "#ef4444";
  const step = failedStep(run);
  const canResumeFromCheckpoint =
    run.status === "failed" &&
    !!run.error_detail?.node_id &&
    !run.retry_run_id &&
    run.steps.length > 0 &&
    run.steps[0]?.id !== run.error_detail.node_id;

  const doResume = async () => {
    if (!onRetryFromCheckpoint || resumeBusy) return;
    setResumeBusy(true);
    setResumeErr(null);
    try {
      const r = await onRetryFromCheckpoint();
      if (r && r.ok === false) {
        setResumeErr(r.detail || t("resumeFailed"));
        setResumeBusy(false);
      }
      // ok: stay busy — the refresh swaps this panel for the new run card.
    } catch {
      setResumeErr(tCommon("runtimeUnreachable"));
      setResumeBusy(false);
    }
  };
  const traceback =
    run.error_detail?.traceback ||
    (events || []).filter((e) => e.kind === "error").map((e) => e.traceback).filter(Boolean).pop() ||
    null;

  const ensureEvents = useCallback(async () => {
    if (events || loading) return;
    setLoading(true);
    try {
      const r = await getWorkflowRunEvents(run.id);
      setEvents(r.events || []);
    } catch {
      setEvents([]); // sidecar down — degrade to "no log" copy
    } finally {
      setLoading(false);
    }
  }, [events, loading, run.id]);

  // Opening the panel is the trigger to fetch the evidence behind it.
  useEffect(() => {
    if (open) void ensureEvents();
  }, [open, ensureEvents]);

  useEffect(
    () => () => {
      if (copyTimer.current) clearTimeout(copyTimer.current);
    },
    [],
  );

  if (!FAILURE.has(run.status)) return null;

  const doCopy = async () => {
    // Make sure we have events for the report tail; fetch if the user copied
    // without ever expanding.
    let evs = events;
    if (!evs) {
      try {
        const r = await getWorkflowRunEvents(run.id);
        evs = r.events || [];
        setEvents(evs);
      } catch {
        evs = [];
      }
    }
    const ok = await copyText(buildRunErrorReport(run, evs));
    setCopied(ok ? "copied" : "failed");
    if (copyTimer.current) clearTimeout(copyTimer.current);
    copyTimer.current = setTimeout(() => setCopied("idle"), 2000);
  };

  return (
    <div
      className="mt-2 rounded-md border border-red/30 bg-red/[0.06]"
      style={{ borderColor: run.status === "interrupted" ? "rgba(249,115,22,.35)" : undefined }}
    >
      <div className="flex items-start gap-2 px-2 py-1.5">
        <div className="min-w-0 flex-1 break-words font-mono text-[11px] leading-relaxed" style={{ color }}>
          {run.error || fallbackError(run)}
          {step && (
            <span
              className="ml-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-sans"
              style={{ background: "rgba(239,68,68,.12)", color }}
              title={t("failedStepTitle")}
            >
              {t("failedStep", { title: step.title })}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {canResumeFromCheckpoint && onRetryFromCheckpoint && (
            <button
              onClick={() => void doResume()}
              disabled={resumeBusy}
              className={`flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-card2 hover:text-txt disabled:opacity-50 ${
                resumeErr ? "anim-shake" : ""
              }`}
              title={t("resumeTitle", { node: step?.title || run.error_detail?.node_id || "?" })}
            >
              {resumeBusy ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <RotateCcw className="h-3 w-3" />
              )}
              {resumeBusy ? t("resuming") : t("retryFromStep")}
            </button>
          )}
          <button
            onClick={() => setOpen((o) => !o)}
            className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-card2 hover:text-txt"
            title={t("detailsTitle")}
          >
            {open ? t("collapse") : t("details")}
          </button>
          <button
            onClick={doCopy}
            className="flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-card2 hover:text-txt"
            title={t("copyTitle")}
          >
            {copied === "copied" ? (
              <>
                <Check className="h-3 w-3 text-green" /> {t("copied")}
              </>
            ) : copied === "failed" ? (
              t("copyFailed")
            ) : (
              <>
                <Copy className="h-3 w-3" /> {t("copyReport")}
              </>
            )}
          </button>
        </div>
      </div>
      {resumeErr && <div className="px-2 pb-1.5 text-[10px] text-red">{resumeErr}</div>}

      {open && (
        <div className="space-y-2 border-t border-red/20 px-2 py-2">
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">{t("traceback")}</div>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-base/60 p-2 font-mono text-[10px] leading-relaxed text-muted">
              {traceback || (loading ? tCommon("loading") : t("noTraceback"))}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">
              {events
                ? t("recentEventsCount", { count: Math.min(TAIL_N, events.length) })
                : t("recentEvents")}
            </div>
            {loading ? (
              <div className="py-2 text-center text-[11px] text-faint">{tCommon("loading")}</div>
            ) : (
              <WorkflowLogTimeline events={(events || []).slice(-TAIL_N)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
