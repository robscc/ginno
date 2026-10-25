"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Sparkles } from "lucide-react";
import { connectorAction } from "@/lib/runtime";
import { toolLabelOptions } from "@/lib/toolLabels";
import { parseDelegation } from "./delegation";
import { useChatT } from "./i18n";

// Every tool output is collapsible. Long outputs (over these thresholds)
// default to a compact header row and expand into a capped, internally
// scrolling box; short outputs default to expanded.
const LONG_OUTPUT_LINES = 12;
const LONG_OUTPUT_CHARS = 600;

/** 接管等待卡(browser-companion §7.2 M3):browser_handoff 阻塞期间替换
 * 工具气泡。释放走连接器 action(release-all 语义,无需解析 args)。 */
function BrowserHandoffCard() {
  const [released, setReleased] = useState(false);
  const { tc } = useChatT();
  return (
    <div className="my-1.5 rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-2.5 text-xs">
      <div className="font-medium text-txt">{tc("browserHandoff.title")}</div>
      <div className="mt-0.5 text-faint">
        {tc("browserHandoff.body")}
      </div>
      <button
        disabled={released}
        onClick={async () => {
          setReleased(true);
          try {
            await connectorAction("chrome-extension", "browser_handoff_release");
          } catch { /* sidecar 未起时仅置灰 */ }
        }}
        className="mt-2 rounded-lg bg-violet-600 px-3 py-1.5 font-medium text-white hover:bg-violet-500 disabled:opacity-50"
      >
        {released ? tc("browserHandoff.handedBack") : tc("browserHandoff.continue")}
      </button>
    </div>
  );
}

export function ToolBlock({ name, content, pending, argsPreview }: { name: string; content: string; pending: boolean; argsPreview?: string }) {
  // null = user hasn't toggled yet → default depends on output length
  // (re-evaluated once content arrives, so pending→done stays correct).
  const [open, setOpen] = useState<boolean | null>(null);
  const { tc } = useChatT();
  // A configured label may carry several "|" separated names — pick one at
  // random per tool call (memoized so it stays stable pending → done and
  // across re-renders; a fresh call rolls again).
  const label = useMemo(() => {
    const opts = toolLabelOptions(name);
    return opts[Math.floor(Math.random() * opts.length)];
  }, [name]);
  if (pending && name === "browser_handoff") {
    // 接管卡(browser-companion §7.2 M3):工具在阻塞等待用户,气泡换成
    // 可操作卡片——点「已接管,继续」释放,tool.end 自然接棒。
    return <BrowserHandoffCard />;
  }
  if (pending) {
    return (
      <div className="my-1.5 rounded-md border border-line bg-base/40 px-2.5 py-1.5 font-mono text-xs">
        <span
          className="inline-flex min-w-0 max-w-full items-center gap-1.5 text-faint"
          title={argsPreview ? `${name}\n${argsPreview}` : name}
        >
          <Loader2 className="h-3 w-3 shrink-0 animate-spin" />
          <span className="shrink-0">{label}…</span>
          {argsPreview && <span className="truncate text-muted">· {argsPreview}</span>}
        </span>
      </div>
    );
  }
  const lineCount = content.split("\n").length;
  const del = name === "delegate_agent" ? parseDelegation(content) : null;
  // 机器头与溯源行是给模型/回放跳转看的，展示层剥掉。
  const shown = del
    ? content
        .split("\n")
        .filter((l, idx) => idx > 0 && !/^delegation=/.test(l))
        .join("\n")
        .trim()
    : content;
  // spawn_subagent 的回执是固定模板（"已启动 session_id=… / 它在后台独立运行…"），
  // 每次委派都全展开会把主 agent 的内容挤下去——默认只留标题行，点开看全文
  // （2026-10-01 空间优化）。模型侧拿到的仍是全文，这里只影响显示。
  const isLong =
    name === "spawn_subagent" ||
    name === "delegate_agent" ||
    lineCount > LONG_OUTPUT_LINES ||
    content.length > LONG_OUTPUT_CHARS;
  const expanded = open ?? !isLong;
  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-line bg-base/40 font-mono text-xs">
      <button
        onClick={() => setOpen(!expanded)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left transition-colors hover:bg-card2/50"
        title={`${name} — ${expanded ? tc("tools.collapse") : tc("tools.expand")}`}
      >
        <ChevronRight
          className={`h-3 w-3 shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
        />
        <span className="truncate text-faint" title={argsPreview || undefined}>
          {tc("tools.prefix")}<span className="text-muted">{label}</span>
          {argsPreview && <span> · {argsPreview}</span>}
        </span>
        <span className="shrink-0 text-green">✓</span>
        {del && (
          <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet">
            {del.backend} · {del.mode} · {del.stop}
          </span>
        )}
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {tc("tools.stats", { lines: lineCount, chars: content.length })}
        </span>
      </button>
      {del?.sid && (
        <button
          onClick={() =>
            window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: del.sid }))
          }
          className="block w-full border-t border-line/60 px-2.5 py-1 text-left text-[11px] text-violet transition-colors hover:bg-card2/50"
        >
          {tc("tools.openDelegation")} ↗
        </button>
      )}
      {expanded && (
        <div className={`border-t border-line/60 ${isLong ? "max-h-80 overflow-y-auto" : ""}`}>
          <pre className="overflow-x-auto whitespace-pre-wrap px-2.5 py-1.5 text-faint">{shown}</pre>
        </div>
      )}
    </div>
  );
}

/**
 * Extended-thinking panel: visually distinct (accent border + tinted bg),
 * streams with a pulsing "思考中…" header, and auto-collapses once the turn
 * completes — click to re-read the full reasoning in a capped scroll box.
 */
export function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(true);
  const wasLive = useRef(live);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { tc } = useChatT();
  // "Sticky bottom": keep pinned to the newest line while thinking streams in.
  // If the user scrolls up to read earlier reasoning we stop yanking them back
  // down; scrolling to the bottom re-engages the auto-follow.
  const stickToBottom = useRef(true);

  useEffect(() => {
    if (wasLive.current && !live) setOpen(false); // collapse when thinking finishes
    wasLive.current = live;
  }, [live]);

  // Re-engage auto-follow whenever the panel is (re)opened.
  useEffect(() => {
    if (open) stickToBottom.current = true;
  }, [open]);

  // While streaming, follow the newest line unless the user scrolled away.
  useEffect(() => {
    if (!live || !open) return;
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [text, live, open]);

  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    // "At bottom" within a small threshold so a tiny overshoot still counts.
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  return (
    <div className="my-2 overflow-hidden rounded-r-lg border-l-2 border-violet/70 bg-violet/[0.07]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        title={open ? tc("thinking.collapse") : tc("thinking.expand")}
      >
        <Sparkles className={`h-3.5 w-3.5 shrink-0 text-violet ${live ? "animate-pulse" : ""}`} />
        <span className="text-xs font-medium text-violet">
          {live ? tc("thinking.live") : tc("thinking.done")}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px] text-faint">
          {!live && <span>{tc("thinking.chars", { count: text.length })}</span>}
          <ChevronDown className={`h-3 w-3 transition-transform ${open ? "" : "-rotate-90"}`} />
        </span>
      </button>
      {open && (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="max-h-60 overflow-y-auto border-t border-violet/15 px-3 py-2"
        >
          <div className="selectable whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">{text}</div>
        </div>
      )}
    </div>
  );
}
