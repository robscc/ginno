"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { getWorkflowRunEvents } from "@/lib/runtime";
import { buildRunErrorReport, copyText, failedStep } from "@/lib/errorReport";
import { WorkflowLogTimeline } from "./WorkflowLogTimeline";

const FAILURE = new Set(["failed", "interrupted", "cancelled"]);
const TAIL_N = 15;

function fallbackError(run: WorkflowRun): string {
  if (run.status === "interrupted") return "进程重启导致中断，可重试";
  if (run.status === "cancelled") return "已取消";
  return "执行失败";
}

/**
 * Shared failure panel for a workflow run (work item C). Replaces the old
 * one-line red box: shows the one-line error + the failed step, and expands to
 * the trimmed traceback + the last events leading up to the failure. A "复制
 * 错误报告" button packages all of it as Markdown for pasting into Claude Code.
 *
 * Data: traceback comes from ``run.error_detail`` when present; otherwise (or
 * for the event tail / report) events are lazy-loaded on first expand.
 */
export function RunErrorBox({ run, defaultOpen = false }: { run: WorkflowRun; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [events, setEvents] = useState<WorkflowRunEvent[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState<"idle" | "copied" | "failed">("idle");
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const color = run.status === "interrupted" ? "#f97316" : "#ef4444";
  const step = failedStep(run);
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
              title="失败步骤（按 error_detail.node_id 归因）"
            >
              失败步骤：{step.title}
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button
            onClick={() => setOpen((o) => !o)}
            className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-card2 hover:text-txt"
            title="展开/收起 traceback 与最近事件"
          >
            {open ? "收起 ▾" : "详情 ▸"}
          </button>
          <button
            onClick={doCopy}
            className="flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:bg-card2 hover:text-txt"
            title="复制完整错误报告（Markdown），可直接粘贴给 Claude Code 定位"
          >
            {copied === "copied" ? (
              <>
                <Check className="h-3 w-3 text-green" /> 已复制
              </>
            ) : copied === "failed" ? (
              "复制失败"
            ) : (
              <>
                <Copy className="h-3 w-3" /> 复制错误报告
              </>
            )}
          </button>
        </div>
      </div>

      {open && (
        <div className="space-y-2 border-t border-red/20 px-2 py-2">
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">Traceback</div>
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-base/60 p-2 font-mono text-[10px] leading-relaxed text-muted">
              {traceback || (loading ? "加载中…" : "（无 traceback——该失败未捕获堆栈，如进程重启/旧版本记录）")}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-faint">
              最近事件{events ? `（最后 ${Math.min(TAIL_N, events.length)} 条）` : ""}
            </div>
            {loading ? (
              <div className="py-2 text-center text-[11px] text-faint">加载中…</div>
            ) : (
              <WorkflowLogTimeline events={(events || []).slice(-TAIL_N)} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
