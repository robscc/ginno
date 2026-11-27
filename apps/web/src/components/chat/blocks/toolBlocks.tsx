"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Copy, Loader2, Sparkles } from "lucide-react";
import { connectorAction } from "@/lib/runtime";
import { copyText } from "../copyText";
import { loadHistoryTool } from "./historyTool";
import type { HistoryToolDetail } from "@/lib/api/sessions";
import { toolLabelOptions } from "@/lib/toolLabels";
import { parseDelegation } from "./delegation";
import { useChatT } from "./i18n";
import { TaskCard, parseBgTaskId } from "../TaskCard";
import { parseShellOutput } from "@/lib/toolOutput";
import { toolFailed, toolInterrupted } from "./processGroups";
import { DiffView } from "./diffView";
import type { EditDiffPayload as ImportDiffPayload, HistoryToolRef } from "./types";

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
    <div data-operation-state={released ? undefined : "waiting"} className="my-1.5 rounded-lg border border-violet-500/40 bg-violet-500/10 px-3 py-2.5 text-xs">
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

export function ToolBlock({ name, content, pending, failed, argsPreview, argsStream, diff, detail, isLatestTurn = true }: { name: string; content: string; pending: boolean; failed?: boolean; argsPreview?: string; argsStream?: string; diff?: ImportDiffPayload; detail?: HistoryToolRef; isLatestTurn?: boolean }) {
  // null = user hasn't toggled yet → default depends on output length
  // (re-evaluated once content arrives, so pending→done stays correct).
  const [open, setOpen] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState<{ key: string; value: HistoryToolDetail }>();
  const [failedKey, setFailedKey] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const { tc } = useChatT();
  const key = detail ? `${detail.sessionId}/${detail.key}` : undefined;
  const value = loaded?.key === key ? loaded?.value : undefined;
  const detailFailed = failedKey !== undefined && failedKey === key;
  const lineCount = detail?.lines ?? content.split("\n").length;
  const charCount = detail?.chars ?? content.length;
  const isLong = name === "spawn_subagent" || name === "delegate_agent" ||
    lineCount > LONG_OUTPUT_LINES || charCount > LONG_OUTPUT_CHARS;
  // The latest edit diff is visible by default even when replay gives us a
  // lightweight header. Other deferred outputs stay closed until opened.
  const expanded = open ?? ((diff ? isLatestTurn : !detail) && !isLong);
  useEffect(() => {
    if (!detail || !expanded || pending || !key || value) return;
    let alive = true;
    loadHistoryTool(detail.sessionId, detail.key).then(
      (result) => { if (alive) setLoaded({ key, value: result }); },
      () => { if (alive) setFailedKey(key); },
    );
    return () => { alive = false; };
  }, [expanded, pending, key, detail, value, attempt]);
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
  // bg_run 的气泡升级为常驻 TaskCard（background-tasks-design §6.1，评审
  // 决策 4）：卡片横跨 turn 存活、状态由 /api/tasks 轮询驱动，不随 tool.end
  // 折叠。pending 期间 bg_run 毫秒级返回，走通用 spinner 即可。
  if (name === "bg_run") {
    const taskId = parseBgTaskId(content);
    if (taskId) return <TaskCard taskId={taskId} />;
    // 结果里没有机器头（错误回执等）→ 通用工具气泡渲染 [error] 原文。
  }
  if (pending) {
    // 流式参数预览（2026-11-21）：终态 argsPreview 未到时显示 argsStream
    // （tool.args_delta 的累计尾部快照）——「能显示多少信息就显示多少」，
    // 长参数生成期不再只有 "…" 干等。终态到达后由 reducer 清空回 argsPreview。
    const live = argsPreview ?? argsStream;
    return (
      // 2026-11-11 样式打磨：左缘琥珀状态条 + 轻呼吸光——比纯灰框更有
      // 「正在执行」的存在感，又不至于抢正文注意力。
      <div className="relative my-1.5 overflow-hidden rounded-lg border border-line bg-base/40 py-1.5 pl-3.5 pr-2.5 font-mono text-xs transition-colors hover:border-line2">
        <span className="absolute inset-y-1 left-1 w-0.5 animate-pulse rounded-full bg-amber-500/70" />
        <span
          className="inline-flex min-w-0 max-w-full items-center gap-1.5 text-faint"
          title={live ? `${name}\n${live}` : name}
        >
          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-amber-500/80" />
          <span className="shrink-0 text-muted">{label}…</span>
          {live && <span className="truncate text-faint">· {live}</span>}
        </span>
      </div>
    );
  }
  const body = value?.content ?? content;
  const bodyDiff = value?.diff ?? diff;
  const shell = parseShellOutput(body);
  const del = name === "delegate_agent" ? parseDelegation(content) : null;
  // 机器头与溯源行是给模型/回放跳转看的，展示层剥掉。
  const shown = del
    ? body
        .split("\n")
        .filter((l, idx) => idx > 0 && !/^delegation=/.test(l))
        .join("\n")
        .trim()
    : body;
  const fail = toolFailed(content, failed);
  const interrupted = !fail && toolInterrupted(content);
  const reason = fail ? shell?.stderr?.trim().split("\n")[0] || content.trim().split("\n")[0] : "";
  return (
    // 2026-11-11 样式打磨：状态色左缘条（绿/红一眼分）+ 圆角升级 + 状态
    // 点图标化；hover 边框反馈。视觉层级：条 > 标签 > 预览 > 统计。
    <div
      data-operation-state={fail ? "failed" : interrupted ? "interrupted" : undefined}
      className={`group relative my-1.5 overflow-hidden rounded-lg border bg-base/40 font-mono text-xs transition-colors hover:border-line2 ${
        fail ? "border-red-500/30" : "border-line"
      }`}
    >
      {!fail && !interrupted && (
        <span aria-hidden className="absolute inset-y-1 left-1 w-0.5 rounded-full bg-emerald-500/50" />
      )}
      <button
        type="button" aria-expanded={expanded} data-reveal-record
        onClick={() => { setFailedKey(undefined); setOpen(!expanded); }}
        className="flex w-full cursor-pointer items-center gap-1.5 py-1.5 pl-3.5 pr-2.5 text-left transition-colors hover:bg-card2/50"
        title={`${name} — ${expanded ? tc("tools.collapse") : tc("tools.expand")}`}
      >
        <ChevronRight
          className={`h-3 w-3 shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
        />
        <span className="min-w-0 flex-1 truncate text-faint" title={argsPreview || undefined}>
          {tc("tools.prefix")}<span className="font-medium text-txt/80">{label}</span>
          {argsPreview && <span> · {argsPreview}</span>}
        </span>
        {interrupted && <span className="shrink-0 text-faint">{tc("process.interrupted")}</span>}
        {reason && <span className="max-w-[42%] truncate text-red-500" title={reason}>{reason}</span>}
        {diff && (
          <span className="flex shrink-0 gap-1 font-semibold text-[11px]">
            <span className="text-emerald-600 dark:text-emerald-400">+{diff.added}</span>
            <span className="text-red-600 dark:text-red-400">−{diff.removed}</span>
          </span>
        )}
        <span
          className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] leading-none ${
            fail
              ? "bg-red-500/15 text-red-500"
              : interrupted ? "bg-card2 text-faint" : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
          }`}
        >
          {fail ? "✗" : interrupted ? "–" : "✓"}
        </span>
        {del && (
          <span className="shrink-0 rounded-full border border-violet/40 bg-violet/10 px-1.5 text-[10px] leading-4 text-violet">
            {del.backend} · {del.mode} · {del.stop}
          </span>
        )}
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {tc("tools.stats", { lines: lineCount, chars: charCount })}
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
      {expanded && detail && !value && (detailFailed ? (
        <button type="button" className="px-2.5 py-1 text-red" onClick={() => {
          setFailedKey(undefined);
          setAttempt((n) => n + 1);
        }}>{tc("history.retryOutput")}</button>
      ) : <div data-tool-detail-loading role="status" className="px-2.5 py-1 text-faint">{tc("history.loadingOutput")}</div>)}
      {expanded && (!detail || value) && (
        <div className={`border-t border-line/60 ${isLong ? "max-h-80 overflow-y-auto" : ""}`}>
          {body && (
            <button type="button" aria-label={tc("history.copyOutput")} title={tc("history.copyOutput")}
              onClick={() => void copyText(body)} className="ml-2 mt-1 rounded p-1 text-faint hover:text-txt">
              <Copy className="h-3 w-3" />
            </button>
          )}
          {bodyDiff && <DiffView diff={bodyDiff} />}
          {(() => {
            const sh = shell;
            if (!sh) return <pre className="overflow-x-auto whitespace-pre-wrap px-2.5 py-1.5 text-faint">{shown}</pre>;
            return (
              <div className="px-2.5 py-1.5">
                <div className={sh.ok ? "text-emerald-600/90 dark:text-emerald-400/90" : "text-red-500 dark:text-red-400"}>
                  [{sh.exitLine}]
                </div>
                {sh.stdout.trim() && (
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-faint">{sh.stdout}</pre>
                )}
                {sh.stderr != null && sh.stderr.trim() && (
                  <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-amber-600/90 dark:text-amber-400/80">
                    {sh.stderr}
                  </pre>
                )}
              </div>
            );
          })()}
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
        type="button" aria-expanded={open} data-reveal-record
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
