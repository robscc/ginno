"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight, Loader2, Sparkles } from "lucide-react";
import { toolLabel } from "@/lib/toolLabels";
import { useChatT } from "./i18n";
import type { Block } from "./types";
import { processFileName, processStatus } from "./processGroups";

export const REVEAL_PROCESS_BLOCK = "ginno:reveal-process-block";

export function ProcessFold({
  blocks, indexes, streaming, tail, offline, onInspect, children,
}: {
  blocks: Block[];
  indexes: number[];
  streaming: boolean;
  tail: boolean;
  offline: boolean;
  onInspect?: () => void;
  children: ReactNode;
}) {
  const { tc } = useChatT();
  const id = useId();
  const root = useRef<HTMLElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const anchor = useRef<number | null>(null);
  const [choice, setChoice] = useState<boolean | null>(null);
  const { completed, pending, count, thinking, compact } = processStatus(blocks, streaming, tail);
  const running = streaming && pending.length > 0;
  const active = running || thinking;
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
  const title = offline && active ? tc("process.offline")
    : pending.length > 1 && running ? tc("process.parallel", { count: pending.length })
    : running ? tc("process.running", { action })
    : thinking ? tc("process.thinking")
    : count ? tc("process.completed", { count }) : tc("process.reviewed");
  const stats = (["read", "edit", "command", "other"] as const).flatMap((key) =>
    completed[key] ? [tc(`process.${key}`, { count: completed[key] })] : [],
  ).join(" · ");

  return (
    <section ref={root} data-process-blocks={indexes.join(" ")}
      data-process-fold={folded ? "" : undefined}
      data-process-active={active ? "" : undefined}
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
          {active && !offline && (running
            ? <Loader2 aria-hidden className="h-3 w-3 shrink-0 text-amber-500 motion-safe:animate-spin" />
            : <Sparkles aria-hidden className="h-3 w-3 shrink-0 text-violet motion-safe:animate-pulse" />)}
          <span className="min-w-0 flex-1 truncate font-medium text-txt/80">{title}</span>
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
