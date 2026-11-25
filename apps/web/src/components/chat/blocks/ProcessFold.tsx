"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AlertCircle, ChevronRight, HelpCircle, Loader2, Sparkles } from "lucide-react";
import { toolLabel } from "@/lib/toolLabels";
import { useChatT } from "./i18n";
import type { Block } from "./types";
import { processFileName, processStatus } from "./processGroups";

export const REVEAL_PROCESS_BLOCK = "ginno:reveal-process-block";

export function ProcessFold({
  blocks, indexes, streaming, tail, offline, parked, onInspect, attention, children,
}: {
  blocks: Block[];
  indexes: number[];
  streaming: boolean;
  tail: boolean;
  offline: boolean;
  parked: boolean;
  onInspect?: () => void;
  /** Reports that must stay visible and render right below this group (failed /
   *  waiting for the user). Counted in the header so the chevron never looks
   *  like "everything went fine". */
  attention?: { failed: number; waiting: number };
  children: ReactNode;
}) {
  const { tc } = useChatT();
  const id = useId();
  const root = useRef<HTMLElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const anchor = useRef<number | null>(null);
  const [choice, setChoice] = useState<boolean | null>(null);
  const { completed, pending, count, failed, thinking, compact, reports, planUpdates, plan } =
    processStatus(blocks, streaming, tail);
  const running = streaming && pending.length > 0;
  const active = running || thinking;
  const state = active ? (offline ? "interrupted" : parked ? "waiting" : "running")
    : failed ? "failed" : "completed";
  const open = choice === true;
  // Keep the container and its key stable across deltas and short-group transitions.
  const folded = compact || active || choice !== null;

  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const reveal = () => {
      onInspect?.();
      setChoice(true);
    };
    element.addEventListener(REVEAL_PROCESS_BLOCK, reveal);
    return () => element.removeEventListener(REVEAL_PROCESS_BLOCK, reveal);
  }, [onInspect]);

  useLayoutEffect(() => {
    if (anchor.current == null || !button.current) return;
    const scroll = button.current.closest<HTMLElement>("[data-chat-scroll]");
    if (scroll) scroll.scrollTop += button.current.getBoundingClientRect().top - anchor.current;
    anchor.current = null;
  }, [open]);

  const latest = pending.at(-1);
  const action = latest ? [toolLabel(latest.name), processFileName(latest)].filter(Boolean).join(" ") : "";
  const stepTitle = state === "interrupted" ? tc("process.offline")
    : state === "waiting" ? tc("process.waiting")
    : pending.length > 1 && running ? tc("process.parallel", { count: pending.length })
    : running ? tc("process.running", { action })
    : thinking ? tc("process.thinking")
    : count ? tc("process.completed", { count }) : "";
  // 回报与清单进度接在步数后面：一行读出这一步做了什么、收到几份回报、清单
  // 走到哪（方案 A）。
  const reportTitle = reports ? tc("process.reports", { count: reports })
    : planUpdates ? tc("process.planUpdates", { count: planUpdates }) : "";
  const planTitle = plan ? (plan.total != null
    ? tc("process.planProgress", { done: plan.done, total: plan.total })
    : tc("process.planProgressOpen", { done: plan.done })) : "";
  const titleParts = [stepTitle, reportTitle, planTitle].filter(Boolean);
  const failureTitle = failed ? tc("process.failedCount", { count: failed }) : "";
  const title = titleParts.join(" · ") || failureTitle || tc("process.reviewed");
  const stats = (["read", "edit", "command", "other"] as const).flatMap((key) =>
    completed[key] ? [tc(`process.${key}`, { count: completed[key] })] : [],
  ).join(" · ");

  return (
    <section ref={root} data-process-blocks={indexes.join(" ")}
      data-process-fold={folded ? "" : undefined}
      data-process-active={active ? "" : undefined}
      data-operation-state={folded ? state : undefined}
      className={folded ? "my-2 min-w-0 rounded-lg border border-line bg-base/40" : "min-w-0"}>
      {folded && (
        <button ref={button} type="button" aria-expanded={open} aria-controls={id}
          title={title}
          onClick={() => {
            onInspect?.();
            anchor.current = button.current?.getBoundingClientRect().top ?? null;
            setChoice(!open);
          }}
          className="flex min-h-8 w-full min-w-0 items-center gap-2 rounded-lg px-2.5 py-1 text-left text-xs text-muted hover:bg-card2/50 focus-visible:outline-violet">
          <ChevronRight aria-hidden className={`h-3 w-3 shrink-0 motion-safe:transition-transform ${open ? "rotate-90" : ""}`} />
          {state === "running" && (running
            ? <Loader2 aria-hidden className="h-3 w-3 shrink-0 animate-spin text-amber-500" />
            : <Sparkles aria-hidden className="h-3 w-3 shrink-0 text-violet" />)}
          {!!failureTitle && titleParts.length === 0 && <AlertCircle aria-hidden className="h-3 w-3 shrink-0 text-red" />}
          <span className="min-w-0 flex-1 truncate font-medium text-txt/80">{title}</span>
          {!!failureTitle && titleParts.length > 0 && (
            <span className="flex shrink-0 items-center gap-1 text-txt">
              <AlertCircle aria-hidden className="h-3 w-3 text-red" />
              {failureTitle}
            </span>
          )}
          {attention && attention.failed > 0 && (
            <span className="flex shrink-0 items-center gap-1 text-txt" title={tc("process.failedTitle")}>
              <AlertCircle aria-hidden className="h-3 w-3 text-red" />
              {tc("process.failedCount", { count: attention.failed })}
            </span>
          )}
          {attention && attention.waiting > 0 && (
            <span className="flex shrink-0 items-center gap-1 text-txt" title={tc("process.waitingTitle")}>
              <HelpCircle aria-hidden className="h-3 w-3 text-amber" />
              {tc("process.waitingCount", { count: attention.waiting })}
            </span>
          )}
          <span className="max-w-[60%] shrink-0 truncate text-[10px] text-faint" title={stats || undefined}>{active ? tc("process.completedCount", { count }) : stats}</span>
        </button>
      )}
      <div id={id} hidden={folded && !open}
        className={folded ? "max-h-72 min-w-0 overflow-y-auto overscroll-contain border-t border-line px-2 py-1" : undefined}>
        {(!folded || choice !== null) && children}
      </div>
    </section>
  );
}
