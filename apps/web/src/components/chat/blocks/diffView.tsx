"use client";

// 编辑 diff 显示（2026-11-21 P0/P1）：edit_file / write_file 完成后的
// unified diff 渲染。数据是 tool.end 载荷的独立展示字段（EditDiffPayload，
// 后端 editdiff.py 生成——限 200 行/8KB，added/removed 全量统计）。
// 纯解析函数 parseDiffLines 可单测（diffView.test.ts）；hunk 间 ≥8 行
// 连续上下文折叠成「⋯ N unchanged」可点击展开（P1）。

import { useState } from "react";
import type { EditDiffPayload } from "./types";
import { useChatT } from "./i18n";

export type DiffLine =
  | { t: "hunk"; text: string }
  | { t: "ctx"; ln1: number | null; ln2: number | null; text: string }
  | { t: "add"; ln2: number; text: string }
  | { t: "del"; ln1: number; text: string };

const FOLD_CTX = 8;

/** unified diff 文本 → 行模型（双列行号）。容错：非 diff 行按 ctx 处理。 */
export function parseDiffLines(text: string): DiffLine[] {
  const out: DiffLine[] = [];
  let l1 = 0;
  let l2 = 0;
  let inHunk = false;
  for (const raw of text.split(/\r?\n/)) {
    if (!raw || raw.startsWith("---") || raw.startsWith("+++")) continue;
    if (raw.startsWith("@@")) {
      inHunk = true;
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw);
      if (m) {
        l1 = parseInt(m[1], 10);
        l2 = parseInt(m[2], 10);
      }
      out.push({ t: "hunk", text: raw });
      continue;
    }
    if (!inHunk) continue;
    if (raw.startsWith("+")) out.push({ t: "add", ln2: l2++, text: raw.slice(1) });
    else if (raw.startsWith("-")) out.push({ t: "del", ln1: l1++, text: raw.slice(1) });
    else out.push({ t: "ctx", ln1: l1++, ln2: l2++, text: raw.slice(1) });
  }
  return out;
}

/** 把连续 ≥FOLD_CTX 的 ctx 段折叠为 {fold} 占位（除段首段尾各留 2 行）。 */
export type FoldSpan = { kind: "lines"; items: DiffLine[] } | { kind: "fold"; n: number; items: DiffLine[] };

export function foldContextSpans(lines: DiffLine[]): FoldSpan[] {
  const spans: FoldSpan[] = [];
  let buf: DiffLine[] = [];
  const flush = () => {
    if (buf.length) {
      spans.push({ kind: "lines", items: buf });
      buf = [];
    }
  };
  let i = 0;
  while (i < lines.length) {
    if (lines[i].t === "ctx") {
      let j = i;
      while (j < lines.length && lines[j].t === "ctx") j++;
      const n = j - i;
      if (n >= FOLD_CTX) {
        const head = lines.slice(i, i + 2);
        const tail = lines.slice(j - 2, j);
        const mid = lines.slice(i + 2, j - 2);
        flush();
        spans.push({ kind: "lines", items: head });
        spans.push({ kind: "fold", n: mid.length, items: mid });
        spans.push({ kind: "lines", items: tail });
      } else {
        buf.push(...lines.slice(i, j));
      }
      i = j;
    } else {
      buf.push(lines[i]);
      i++;
    }
  }
  flush();
  return spans;
}

export function DiffView({ diff }: { diff: EditDiffPayload }) {
  const { tc } = useChatT();
  const [unfolded, setUnfolded] = useState<Set<number>>(new Set());
  const [view, setView] = useState<"u" | "s">("u");
  const lines = parseDiffLines(diff.text);
  const spans = foldContextSpans(lines);
  return (
    <div className="text-[11px] leading-relaxed">
      <div className="flex items-center gap-1 border-b border-line/50 px-2 py-0.5">
        <span className="mr-auto text-[10px] text-faint">{diff.path}</span>
        <button
          onClick={() => setView("u")}
          className={`rounded px-1.5 py-0.5 text-[10px] ${view === "u" ? "bg-muted/15 text-txt" : "text-faint hover:text-muted"}`}
        >
          {tc("diff.viewUnified")}
        </button>
        <button
          onClick={() => setView("s")}
          className={`rounded px-1.5 py-0.5 text-[10px] ${view === "s" ? "bg-muted/15 text-txt" : "text-faint hover:text-muted"}`}
        >
          {tc("diff.viewSplit")}
        </button>
      </div>
      {view === "u" ? (
        <div className="diff min-w-0">
          {spans.map((s, i) =>
            s.kind === "fold" ? (
              unfolded.has(i) ? (
                <div key={i}>{s.items.map((l, k) => (
                  <DiffRow key={k} l={l} />
                ))}</div>
              ) : (
                <button
                  key={i}
                  onClick={() => setUnfolded((prev) => new Set(prev).add(i))}
                  className="flex w-full items-center gap-2 bg-muted/5 px-3 py-0.5 text-left text-[10.5px] text-faint hover:text-muted"
                >
                  <span className="tracking-widest">⋯</span> {tc("diff.unchangedLines", { n: s.n })}
                </button>
              )
            ) : (
              <div key={i}>{s.items.map((l, k) => (
                <DiffRow key={k} l={l} />
              ))}</div>
            )
          )}
        </div>
      ) : (
        <SplitView lines={lines} />
      )}
      {diff.truncated && (
        <div className="border-t border-dashed border-line px-3 py-1 text-[10.5px] text-faint">
          {tc("diff.truncatedNote")}
        </div>
      )}
    </div>
  );
}

/** unified 行模型 → split 配对行：连续 del 段与紧随 add 段按位 zip，
 * ctx 双侧同现，未配上的单侧占空。 */
export function pairLines(lines: DiffLine[]): { left: DiffLine | null; right: DiffLine | null }[] {
  const rows: { left: DiffLine | null; right: DiffLine | null }[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.t === "hunk") {
      rows.push({ left: l, right: l });
      i++;
      continue;
    }
    if (l.t === "ctx") {
      rows.push({ left: l, right: l });
      i++;
      continue;
    }
    if (l.t === "del") {
      const dels: DiffLine[] = [];
      while (i < lines.length && lines[i].t === "del") dels.push(lines[i++]);
      const adds: DiffLine[] = [];
      while (i < lines.length && lines[i].t === "add") adds.push(lines[i++]);
      const n = Math.max(dels.length, adds.length);
      for (let k = 0; k < n; k++) rows.push({ left: dels[k] ?? null, right: adds[k] ?? null });
      continue;
    }
    if (l.t === "add") {
      rows.push({ left: null, right: l });
      i++;
      continue;
    }
    i++;
  }
  return rows;
}

function SplitView({ lines }: { lines: DiffLine[] }) {
  return (
    <div className="diff min-w-0">
      {pairLines(lines).map((r, k) => {
        if (r.left?.t === "hunk") {
          return (
            <div key={k} className="bg-indigo-500/5 py-0.5 pl-2 text-[10px] text-indigo-400">
              {r.left.text}
            </div>
          );
        }
        return (
          <div key={k} className="grid grid-cols-2">
            <SplitCell l={r.left} kind="del" />
            <SplitCell l={r.right} kind="add" />
          </div>
        );
      })}
    </div>
  );
}

function SplitCell({ l, kind }: { l: DiffLine | null; kind: "del" | "add" }) {
  const active = l?.t === kind;
  const cls = active
    ? kind === "add"
      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
      : "bg-red-500/10 text-red-600 dark:text-red-400"
    : l?.t === "ctx"
      ? "text-muted"
      : "text-transparent";
  const ln = l && l.t !== "hunk" ? String(l.t === "del" ? l.ln1 : l.t === "add" ? l.ln2 : l.ln1 ?? "") : "";
  return (
    <div className={`grid min-w-0 grid-cols-[3em_minmax(0,1fr)] ${cls}`}>
      <span className="pr-1.5 text-right text-faint select-none">{ln}</span>
      <span className="whitespace-pre-wrap pr-2 [overflow-wrap:anywhere]">
        {l && l.t !== "hunk" ? l.text || "\u00A0" : "\u00A0"}
      </span>
    </div>
  );
}

function DiffRow({ l }: { l: DiffLine }) {
  if (l.t === "hunk") {
    return (
      <div className="bg-indigo-500/5 py-0.5 pl-2 text-indigo-400">{l.text}</div>
    );
  }
  const cls =
    l.t === "add"
      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
      : l.t === "del"
        ? "bg-red-500/10 text-red-600 dark:text-red-400"
        : "text-muted";
  const mk = l.t === "add" ? "+" : l.t === "del" ? "−" : " ";
  const ln1 = l.t === "add" ? "" : String(l.ln1 ?? "");
  const ln2 = l.t === "del" ? "" : String(l.ln2 ?? "");
  return (
    <div className={`grid min-w-0 grid-cols-[3em_3em_1em_minmax(0,1fr)] ${cls}`}>
      <span className="pr-1.5 text-right text-faint select-none">{ln1}</span>
      <span className="pr-1.5 text-right text-faint select-none">{ln2}</span>
      <span className="select-none">{mk}</span>
      <span className="whitespace-pre-wrap pr-3 [overflow-wrap:anywhere]">{l.text || "\u00A0"}</span>
    </div>
  );
}
