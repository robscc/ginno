"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Sparkles } from "lucide-react";
import { useChatT } from "./i18n";
import type { Block } from "./types";

function splitPart(text: string) {
  const trimmed = text.trim();
  const heading = /^\*\*([^\n]+?)\*\*/.exec(trimmed);
  if (heading) return { title: heading[1], body: trimmed.slice(heading[0].length).trim() };
  const [first, ...rest] = trimmed.split("\n");
  return { title: first.replace(/^\*\*|\*\*?$/g, ""), body: rest.join("\n").trim() };
}

export function ReasoningSummary({ block, live, offline }: {
  block: Extract<Block, { kind: "reasoning_summary" }>;
  live: boolean;
  offline: boolean;
}) {
  const { tc } = useChatT();
  const id = useId();
  const [choice, setChoice] = useState<boolean | null>(null);
  const active = live && block.receiving === true;
  const open = choice ?? active;
  const follow = useRef(true);
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    if (open && active && follow.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [block.parts, open, active]);
  // Some gateways put several adjacent bold-only headings in one summary_text.
  // Keep the provider part count and show each heading on its own line.
  const parts = block.parts.filter(p => p.text.trim()).map(p => {
    const text = p.text.replace(/(\*\*[^*\n]+?\*\*)(?=\*\*[^*\n]+?\*\*)/g, "$1\n");
    const { title, body } = splitPart(text);
    return { id: p.id, title, body: body.replace(/^\*\*([^*\n]+)\*\*$/gm, "$1") };
  });
  if (!parts.length) return null;
  const disconnected = active && offline;
  const receiving = active && !offline;
  return (
    <section className={`my-2 min-w-0 overflow-hidden rounded-r-lg border-l-2 ${disconnected ? "border-amber-500/70 bg-amber-500/[0.07]" : "border-violet/70 bg-violet/[0.07]"}`}>
      <button type="button" data-reveal-record aria-expanded={open} aria-controls={id}
        onClick={() => { follow.current = true; setChoice(!open); }}
        className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left hover:bg-card2/40 focus-visible:outline-violet">
        <Sparkles aria-hidden className={`h-3.5 w-3.5 shrink-0 text-violet ${receiving ? "motion-safe:animate-pulse" : ""}`} />
        <strong className={`shrink-0 whitespace-nowrap text-xs font-medium text-violet ${open ? "flex-1" : ""}`}>{tc("reasoningSummary.title")}</strong>
        {!open && <span className="min-w-0 flex-1 truncate text-xs text-muted">{parts.at(-1)?.title}</span>}
        {active && <span className="shrink-0 whitespace-nowrap text-[11px] text-muted">
          {tc(disconnected ? "reasoningSummary.offline" : "reasoningSummary.receiving")}
        </span>}
        <span className="shrink-0 whitespace-nowrap text-[11px] tabular-nums text-muted">{tc("reasoningSummary.count", { count: parts.length })}</span>
        <ChevronDown aria-hidden className={`h-3 w-3 shrink-0 text-faint ${open ? "" : "-rotate-90"}`} />
      </button>
      <div id={id} hidden={!open} className="border-t border-line">
        <ol ref={list} tabIndex={0} aria-label={tc("reasoningSummary.title")} className="selectable max-h-60 space-y-2.5 overflow-y-auto px-3 py-2.5"
          onScroll={e => { const el = e.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24; }}>
          {parts.map(({ id, title, body }, index) => <li key={id} className="flex items-start gap-2 break-words text-xs">
            <span aria-hidden className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${receiving && index === parts.length - 1 ? "bg-violet" : "bg-violet/40"}`} />
            <div className="min-w-0"><strong className="font-medium text-txt">{title}</strong>{body && <p className="mt-0.5 whitespace-pre-wrap leading-relaxed text-muted">{body}</p>}</div>
          </li>)}
        </ol>
        {disconnected && <p className="px-3 pb-2 text-xs text-muted">{tc("reasoningSummary.offlineHint")}</p>}
      </div>
    </section>
  );
}
