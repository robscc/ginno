"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import * as d3 from "d3";
import {
  BarChart3,
  BookMarked,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  Code2,
  FileText,
  Flag,
  Globe,
  Link2,
  Loader2,
  RotateCw,
  Sparkles,
  Workflow,
  X,
} from "lucide-react";
import type { WikiPage, WorkflowRun } from "@/lib/types";
import type { CodeRoot } from "@/lib/codeTypes";
import { fileDownloadUrl, listCodeRoots } from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import { isSubagentConfirmed, markSubagentConfirmed } from "@/lib/subagentConfirm";
import { Markdown } from "./Markdown";
import { cn } from "@/lib/utils";
import { AskUserCard } from "./AskUserCard";
import { toolLabel } from "@/lib/toolLabels";

export type SourceItem = { kind: "wiki" | "web"; ref: string; note?: string };

export type Block =
  | { kind: "text"; text: string }
  // `url` is set for user uploads (data URL). Code-generated images carry a
  // file-ledger `fileId` (+ mtime for cache-busting) resolved via imageUrl().
  | { kind: "image"; url?: string; fileId?: string; name?: string; mtime?: number }
  | { kind: "file"; fileId?: string; name: string; path?: string; fileKind?: string }
  // Slash-skill invocation (/name …): the persisted HumanMessage carries the
  // SKILL.md injection; history replay folds it into this chip + user request.
  | { kind: "skill"; name: string; text?: string }
  | { kind: "widget"; widgetKind: string; data: unknown; renderId?: string }
  | { kind: "ref"; refKind: string; name: string; refId?: string }
  | { kind: "tool"; id?: string; name: string; content: string; pending: boolean; argsPreview?: string }
  // ask_user parked the turn on an ambiguity: the card REPLACES the pending
  // ask_user tool bubble (same tool-call id), and the tool's JSON result folds
  // back into status/answer on tool.end (a stop while parked → "skipped").
  // History replay rebuilds it from the checkpoint (api/messages_ui.py).
  | { kind: "question"; id?: string; question: string; header?: string;
      options: string[]; allowFreeText: boolean;
      status: "pending" | "answered" | "skipped";
      answer?: string; optionIndex?: number | null }
  | { kind: "thinking"; text: string }
  | { kind: "workflow"; run: WorkflowRun }
  // WorldState change announcements (docs/design/world-state-plan.md §7):
  // centered system rows in the transcript ("context chips").
  | { kind: "context"; text: string }
  // Answer provenance (docs/citations-design.md): wiki pages / web sources the
  // model cited. Server emits this on history replay; live text blocks are
  // parsed client-side (the trailing <ginno_citations> block is machine meta).
  | { kind: "sources"; items: SourceItem[] }
  // Mid-turn steering (docs/steering-design.md §4.2): a message the user sent
  // while the turn was running, absorbed at a tool-batch boundary. It renders
  // as a full-width band INSIDE the assistant bubble it interrupted — one turn
  // is one bubble, and the band is that injection point made visible. Claude
  // Code shows the same thing as a full-width highlight (changelog 2.1.181).
  // Attachments ride the band (steer-attachments v2). Both the live absorbed
  // band (ChatStream) and the server's replay summary use this shape: images
  // carry a display URL (data URL) when available, files a name (+ optional
  // id/path/kind). Kept tolerant — a summary may omit the image payload.
  | { kind: "steer"; text: string; steerId?: string | null; injectedAt?: number;
      images?: SteerBandImage[]; files?: SteerBandFile[] }
  // ---- subagent（subagent-design.md §6.3，P1 web 侧）----
  // 发起卡片：由 subagent.spawned WS 事件驱动（live 追加，不持久化——持久层
  // 里的 spawn_subagent 工具气泡仍然可见）。状态从 store 的会话元数据实时读。
  | { kind: "subagent_spawn"; sessionId: string; goal: string; constraints?: string;
      acceptance?: string; title?: string; depth?: number;
      origin?: "user" | "agent"; spawnedAt?: number }
  // 结果卡片：由注入的 <ginno_subagent_result> 消息（历史重放折行）与
  // subagent.status 终态事件（live 追加）驱动。
  | { kind: "subagent_result"; sessionId: string; goal?: string; summary: string;
      error?: string }
  // 任务简报卡片：子会话首条消息是 <ginno_subagent_brief> 原文（后接报告
  // 格式与输出纪律两段附录），历史重放折成卡片，避免裸 XML 长文刷屏。
  | { kind: "subagent_brief"; goal: string; constraints?: string;
      acceptance?: string; fork?: boolean; notes?: string }
  // 拆分方案卡片（P2 共享契约 1/2）：由 subagent.plan WS 事件驱动（live 追加，
  // 不持久化——未确认的方案没有落 checkpoint 的意义）。组件本体在
  // subagentPlanCard.tsx（本文件已超 1600 行，卡片不再往里塞）。
  | { kind: "subagent_plan"; planId: string; task: string;
      subtasks: import("@/lib/types").SubagentPlanSubtask[];
      status: "pending" | "confirmed" | "cancelled" };

/** 子代状态点（契约用 emoji：🟢 running / ⏳ waiting / ✅ done / ⚠ failed / ⛔ stopped）。
 *  侧栏行与卡片共用同一份映射。 */
export const SUBAGENT_STATUS_META: Record<
  string,
  { glyph: string; label: string; color: string }
> = {
  running: { glyph: "🟢", label: "运行中", color: "#22c55e" },
  waiting: { glyph: "⏳", label: "等待子任务", color: "#eab308" },
  done: { glyph: "✅", label: "已完成", color: "#22c55e" },
  failed: { glyph: "⚠️", label: "失败", color: "#ef4444" },
  stopped: { glyph: "⛔", label: "已停止", color: "#71717a" },
};

export function subagentStatusMeta(status?: string) {
  return SUBAGENT_STATUS_META[status ?? ""] ?? SUBAGENT_STATUS_META.running;
}

/** 子代理的 fork / 类型小徽标（P3 范围 3）：meta.subagent.mode === "fork" 时
 *  显示 fork 徽标；agent_type 仅在 runtime 侧确实把类型名记进了 meta 时渲染
 *  （防御性读取，不造字段——没记就什么都不显示）。发起卡片、结果卡片与侧栏
 *  子会话行共用同一份渲染。 */
export function SubagentKindBadges({
  sub,
}: { sub?: import("@/lib/types").SubagentMeta | null }) {
  if (!sub) return null;
  const agentType = (sub as { agent_type?: unknown }).agent_type;
  return (
    <>
      {sub.mode === "fork" && (
        <span
          className="shrink-0 rounded-md border border-violet/40 bg-violet/10 px-1 py-px text-[10px] leading-4 text-violet"
          title="fork：从父对话的完整上下文分出的并行分支"
        >
          fork
        </span>
      )}
      {typeof agentType === "string" && agentType && (
        <span
          className="shrink-0 rounded-md border border-line2 bg-card2/60 px-1 py-px text-[10px] leading-4 text-muted"
          title={`子代理类型：${agentType}`}
        >
          {agentType}
        </span>
      )}
    </>
  );
}

/** 解析契约 3 的注入消息文本（整个 text 块恰为一个结果标签时才算命中）。
 *  属性名容忍乱序；正文是 result_summary 原文。属性值经 runtime 侧
 *  _xml_attr 转义（goal 是模型自由文本，引号不转义会截断属性），在此还原。 */
const SUBAGENT_RESULT_RE =
  /^<\s*ginno_subagent_result\b([^>]*)>([\s\S]*?)<\s*\/\s*ginno_subagent_result\s*>$/i;

const ATTR_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  "#39": "'",
};

function unescapeAttr(value: string): string {
  return value.replace(/&(amp|lt|gt|quot|#39);/g, (_, ent: string) =>
    ent in ATTR_ENTITIES ? ATTR_ENTITIES[ent] : ent,
  );
}

export function parseSubagentResult(
  text: string,
): { sessionId: string; goal: string; summary: string } | null {
  const m = SUBAGENT_RESULT_RE.exec(text.trim());
  if (!m) return null;
  const attr = (name: string) =>
    unescapeAttr(
      new RegExp(`${name}\\s*=\\s*"([^"]*)"`, "i").exec(m[1])?.[1] ?? "",
    );
  return { sessionId: attr("session"), goal: attr("goal"), summary: m[2].trim() };
}

/** 子会话首条消息的简报标签（runtime subagent_scheduler._build_brief）。
 *  消息主体是 <ginno_subagent_brief> 块 + 报告格式/输出纪律两段附录；
 *  标签外的剩余文本（附录）收进 notes 折叠展示。字段是行式的
 *  「目标：/约束：/验收标准：」，不是 XML 属性，无需转义还原。 */
const SUBAGENT_BRIEF_RE =
  /<\s*ginno_subagent_brief\s*>([\s\S]*?)<\s*\/\s*ginno_subagent_brief\s*>/i;

export function parseSubagentBrief(
  text: string,
): {
  goal: string;
  constraints?: string;
  acceptance?: string;
  fork?: boolean;
  notes?: string;
} | null {
  const t = text.trim();
  // 廉价护栏：简报消息以标签开头，避免对普通含标签文本误伤。
  if (!t.startsWith("<")) return null;
  const m = SUBAGENT_BRIEF_RE.exec(t);
  if (!m) return null;
  const inner = m[1];
  const field = (label: string) =>
    new RegExp(`^\\s*${label}：(.*)$`, "m").exec(inner)?.[1]?.trim() ?? "";
  const goal = field("目标");
  if (!goal) return null;
  const notes = [t.slice(0, m.index), t.slice(m.index + m[0].length)]
    .map((s) => s.trim())
    .filter(Boolean)
    .join("\n\n");
  return {
    goal,
    constraints: field("约束") || undefined,
    acceptance: field("验收标准") || undefined,
    fork: inner.includes("并行分支（fork）"),
    notes: notes || undefined,
  };
}

/** 历史重放用：把持久化的注入消息（HumanMessage 原文）折成结果卡片块，
 *  避免原始 XML 标签以用户气泡形式出现在主对话里；子会话的
 *  <ginno_subagent_brief> 简报消息同样折成简报卡片。 */
export function foldSubagentResultBlocks(blocks: Block[]): Block[] {
  return blocks.map((b) => {
    if (b.kind !== "text") return b;
    const r = parseSubagentResult(b.text);
    if (r) {
      return {
        kind: "subagent_result",
        sessionId: r.sessionId,
        goal: r.goal,
        summary: r.summary,
      };
    }
    const br = parseSubagentBrief(b.text);
    if (br) return { kind: "subagent_brief", ...br };
    return b;
  });
}

export type SubagentCardBlock =
  | Extract<Block, { kind: "subagent_spawn" }>
  | Extract<Block, { kind: "subagent_result" }>
  | Extract<Block, { kind: "subagent_brief" }>;

/** One attachment chip shown inside a steer band. */
export type SteerBandImage = {
  name?: string;
  url?: string;
  data?: string;
  mediaType?: string;
  media_type?: string;
};
export type SteerBandFile = { id?: string; name: string; path?: string; kind?: string };

export type QuestionBlock = Extract<Block, { kind: "question" }>;

/** Resolve an image block to a displayable URL.

User uploads carry a self-contained data URL in `url`. Code-generated images
(bash → file ledger) carry a `fileId` served by the sidecar; the URL is built
through the BASE-aware download helper, with `?t=mtime` busting the browser
cache when a same-named image is regenerated. */
export function imageUrl(b: Extract<Block, { kind: "image" }>): string {
  if (b.url) return b.url;
  if (b.fileId) {
    const t = b.mtime ? `?t=${b.mtime}` : "";
    return `${fileDownloadUrl(b.fileId)}${t}`;
  }
  return "";
}

// Non-global: used by .test()/.match() (a /g regex there would be stateful).
const CITATION_BLOCK_RE =
  /<\s*ginno_(?:wiki_)?citations\s*>([\s\S]*?)<\s*\/\s*ginno_(?:wiki_)?citations\s*>/i;
// Global variants for replace(): strip ALL blocks, including a truncated one
// whose closing tag never arrived (model output cut off inside the block).
const CITATION_BLOCK_RE_G =
  /<\s*ginno_(?:wiki_)?citations\s*>[\s\S]*?<\s*\/\s*ginno_(?:wiki_)?citations\s*>/gi;
const CITATION_UNCLOSED_RE = /<\s*ginno_(?:wiki_)?citations\b[^>]*>[\s\S]*$/i;
const CITATION_OPEN_RE = /<\s*ginno_(?:wiki_)?citations/i;
// web_search tool output lines: "[s1] Title — host\n    https://url"
const WEB_RESULT_RE = /\[(s\d+)\][^\n]*\n\s+(https?:\/\/\S+)/g;

/** Tolerantly parse a trailing ``<ginno_citations>`` block (mirror of the
 * runtime parser — history text is already stripped, this covers live text). */
export function parseSources(text: string): SourceItem[] {
  const m = text.match(CITATION_BLOCK_RE);
  if (!m) return [];
  const items: SourceItem[] = [];
  const seen = new Set<string>();
  for (const raw of m[1].split("\n")) {
    let line = raw.trim();
    if (!line) continue;
    let note = "";
    // Brackets optional: contract is note=[…] but models often emit note=… —
    // accept both so the note splits off and web refs stay clean, openable URLs.
    const nm = line.match(/\|\s*note\s*=\s*\[?(.*?)\]?\s*$/i);
    if (nm) {
      note = nm[1].trim();
      line = line.slice(0, nm.index).trimEnd();
    }
    let kind: string, ref: string;
    const bar = line.indexOf("|");
    if (bar < 0) {
      kind = "wiki";
      ref = line;
    } else {
      kind = line.slice(0, bar).trim().toLowerCase();
      ref = line.slice(bar + 1).trim();
    }
    if ((kind !== "wiki" && kind !== "web") || !ref) continue;
    const key = `${kind}:${ref.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ kind: kind as SourceItem["kind"], ref, note });
    if (items.length >= 20) break;
  }
  return items;
}

export function stripSources(text: string): string {
  return text
    .replace(CITATION_BLOCK_RE_G, "")
    .replace(CITATION_UNCLOSED_RE, "")
    .replace(/\s+$/, "");
}

/** Build an sN → URL map from the web_search tool outputs in a block list.
 * The citation contract lets the model cite by id (`web|s3`); the id only
 * means anything next to the tool result that minted it, so resolve it here
 * (both live transcripts and history carry the tool blocks). */
export function webRefMap(blocks: Block[]): Record<string, string> {
  const map: Record<string, string> = {};
  for (const b of blocks) {
    if (b.kind !== "tool" || b.name !== "web_search" || !b.content) continue;
    WEB_RESULT_RE.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = WEB_RESULT_RE.exec(b.content)) !== null) {
      map[m[1].toLowerCase()] = m[2];
    }
  }
  return map;
}

/** Replace `web|sN` refs with their resolved URL (leaves others untouched). */
export function resolveSourceRefs(items: SourceItem[], map: Record<string, string>): SourceItem[] {
  return items.map((s) => {
    if (s.kind === "web" && /^s\d+$/i.test(s.ref)) {
      const url = map[s.ref.toLowerCase()];
      if (url) return { ...s, ref: url };
    }
    return s;
  });
}

/** While streaming, hide an in-flight (not yet closed) citation block so the
 * raw machine text never flashes; the closed block folds into SourcesBlock. */
export function maskPartialSources(text: string): string {
  if (CITATION_BLOCK_RE.test(text)) return text;
  const m = text.match(CITATION_OPEN_RE);
  return m ? text.slice(0, m.index).replace(/\s+$/, "") : text;
}

/** Centered, de-emphasized system row for context chips. */
export function ContextBlocks({ blocks }: { blocks: Extract<Block, { kind: "context" }>[] }) {
  if (!blocks.length) return null;
  return (
    <div className="flex flex-col items-center gap-1">
      {blocks.map((b, i) => (
        <div
          key={i}
          className="max-w-[85%] whitespace-pre-wrap rounded-lg border border-line/60 bg-card/40 px-3 py-1.5 text-center text-xs leading-relaxed text-muted"
        >
          {b.text}
        </div>
      ))}
    </div>
  );
}

/** Web-hostname label for a citation ref (falls back to the ref itself). */
function hostOf(ref: string): string {
  try {
    return new URL(ref).hostname.replace(/^www\./, "");
  } catch {
    return ref;
  }
}

/** Normalize a wiki ref for matching — frontend mirror of the runtime's
 * `_norm_wiki_ref`: `./` as a SEGMENT prefix, leading slashes, optional `.md`,
 * case-insensitive (models cite paths or titles with drift). */
export function normWikiRef(ref: string): string {
  let r = ref.trim();
  while (r.startsWith("./")) r = r.slice(2);
  r = r.replace(/^\/+/, "");
  if (r.toLowerCase().endsWith(".md")) r = r.slice(0, -3);
  return r.toLowerCase();
}

/** Resolve a cited wiki ref against the KB page list (path first, then title —
 * same precedence the runtime's validate_citations uses). Null = not in KB. */
export function matchWikiPage(pages: WikiPage[], ref: string): WikiPage | null {
  const key = normWikiRef(ref);
  if (key) {
    const byPath = pages.find((p) => normWikiRef(p.path) === key);
    if (byPath) return byPath;
  }
  const titleKey = ref.trim().toLowerCase();
  return pages.find((p) => p.title.trim().toLowerCase() === titleKey) ?? null;
}

/** Answer provenance: cited wiki pages + web sources (citations-design.md §5.2).
 * Collapsed to one line; expands to a list. Web rows open in the system
 * browser (via sidecar — WKWebView won't hand off external links itself);
 * wiki rows deep-link to /kb?page=<path> (resolved against the KB list —
 * frontend-only, mirroring validate_citations' path-then-title matching).
 * Unresolvable refs (index_only citations, deleted pages, model drift)
 * degrade to the old plain row + 「未收录」 badge instead of navigating. */
export function SourcesBlock({ items }: { items: SourceItem[] }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [missed, setMissed] = useState<ReadonlySet<string>>(new Set());
  const router = useRouter();
  if (!items.length) return null;

  const openWeb = async (url: string) => {
    setBusy(url);
    try {
      const { openExternal } = await import("@/lib/runtime");
      const r = await openExternal(url);
      if (!r.ok) window.open(url, "_blank", "noopener");
    } catch {
      window.open(url, "_blank", "noopener");
    } finally {
      setBusy(null);
    }
  };

  const openWiki = async (ref: string) => {
    setBusy(ref);
    try {
      const { kbWikiList } = await import("@/lib/runtime");
      const r = await kbWikiList();
      const target = r.ok ? matchWikiPage(r.pages ?? [], ref) : null;
      if (target) {
        router.push(`/kb?page=${encodeURIComponent(target.path)}`);
      } else {
        // Only "list loaded, no match" is a real miss — a fetch error keeps
        // the row clickable (the page may exist; retry later).
        setMissed((prev) => {
          const next = new Set(prev);
          next.add(ref);
          return next;
        });
      }
    } catch {
      /* sidecar unreachable — leave the row as-is */
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mt-2 rounded-lg border border-line/60 bg-card/40 text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-muted hover:text-txt"
      >
        <Link2 className="h-3.5 w-3.5 shrink-0" />
        <span>来源 · {items.length}</span>
        <ChevronDown className={`ml-auto h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="flex flex-col gap-0.5 border-t border-line/50 px-2 py-1.5">
          {items.map((s, i) => {
            const isWeb = s.kind === "web" && /^https?:\/\//i.test(s.ref);
            const wikiMissed = s.kind === "wiki" && missed.has(s.ref);
            const label = s.kind === "web" ? hostOf(s.ref) : s.ref.split("/").pop() || s.ref;
            const Icon = s.kind === "web" ? Globe : BookMarked;
            return (
              <div
                key={i}
                className="flex items-start gap-2 rounded-md px-1.5 py-1 hover:bg-panel/60"
                title={s.note || s.ref}
              >
                <Icon className={`mt-0.5 h-3.5 w-3.5 shrink-0 ${s.kind === "web" ? "text-blue" : "text-violet"}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {isWeb ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWeb(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-blue disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : s.kind === "wiki" && !wikiMissed ? (
                      <button
                        type="button"
                        disabled={busy === s.ref}
                        onClick={() => openWiki(s.ref)}
                        className="max-w-full truncate text-left text-txt underline decoration-line underline-offset-2 hover:text-violet disabled:opacity-50"
                      >
                        {label}
                      </button>
                    ) : (
                      <span className="min-w-0 max-w-full truncate text-txt">{label}</span>
                    )}
                    {wikiMissed && (
                      <span className="shrink-0 rounded border border-line px-1 text-[10px] leading-4 text-faint">
                        未收录
                      </span>
                    )}
                  </div>
                  {s.note && <div className="truncate text-faint">{s.note}</div>}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

type FileBlock = Extract<Block, { kind: "file" }>;

const TABLE_KINDS = new Set(["spreadsheet", "table"]);

// ---- chip → code-panel routing (docs/code-panel-design.md §3.4, D5) --------
// The DEFAULT click on a chip is unchanged: it still opens the SheetViewer
// preview (tables/PDF/images/document extracts), so chat behaviour is
// byte-for-byte the same. Text/code/data files additionally get an EXPLICIT
// secondary entry ("在代码面板打开"). Should the default ever be re-routed by
// file kind, flip the one switch below — the call sites don't move.
const CODE_CHIP_ROUTE: "preview" | "code" = "preview";
const CODE_FILE_KINDS = new Set(["text", "code", "data"]);

/** Which path a chip's (default) click should take. */
function chipRoute(fileKind: string | undefined): "preview" | "code" {
  if (CODE_CHIP_ROUTE === "code" && CODE_FILE_KINDS.has(fileKind ?? "")) return "code";
  return "preview";
}

/**
 * Secondary entry: open a file chip in the code panel. The chip carries an
 * absolute registry path (uploads land in the session workspace); the panel is
 * addressed by `root id` + root-relative path, so resolve the longest matching
 * root first. A path under no root is handed to the panel, which shows the
 * "该文件不在当前工作区根内" guidance (design §3.2).
 */
async function openChipInCode(
  g: ReturnType<typeof useGinno>,
  file: FileBlock,
): Promise<void> {
  const sessionId = g.activeSessionId;
  const path = file.path ?? "";
  if (!sessionId || !path) return;
  if (!path.startsWith("/")) {
    // Already root-relative — trust the currently selected root.
    g.openInCode({ rootId: g.codeRootId ?? "session", path });
    return;
  }
  let roots: CodeRoot[];
  try {
    roots = await listCodeRoots("default", sessionId);
  } catch {
    return;
  }
  let best: { rootId: string; rel: string } | null = null;
  for (const r of roots) {
    if (!r.path) continue;
    const prefix = r.path.endsWith("/") ? r.path : `${r.path}/`;
    if (path === r.path) {
      best = { rootId: r.id, rel: "" };
      continue;
    }
    if (path.startsWith(prefix)) {
      const rel = path.slice(prefix.length);
      // Most specific root wins = the shortest remaining relative path.
      if (!best || rel.length < best.rel.length) best = { rootId: r.id, rel };
    }
  }
  if (best) {
    g.openInCode({ rootId: best.rootId, path: best.rel });
  } else {
    // Outside every root: let the panel surface the guidance.
    g.openInCode({ rootId: g.codeRootId ?? roots[0]?.id ?? "session", path });
  }
}

/** Clickable file chips (user bubble + replayed history). Default click opens
 *  the preview; text/code/data chips also carry a secondary "open in code
 *  panel" entry. */
export function FileChips({ files }: { files: FileBlock[] }) {
  const g = useGinno();
  if (!files.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {files.map((f, i) => {
        const clickable = !!f.fileId;
        const showCodeEntry = CODE_FILE_KINDS.has(f.fileKind ?? "") && !!f.path;
        return (
          <div
            key={f.fileId ?? `${f.name}-${i}`}
            className={`flex items-center gap-1.5 rounded-lg border border-line bg-card2 px-2 py-1 text-xs text-txt ${
              clickable ? "hover:border-violet/50" : ""
            }`}
          >
            <button
              type="button"
              disabled={!clickable}
              onClick={() => {
                if (!clickable) return;
                if (chipRoute(f.fileKind) === "code") void openChipInCode(g, f);
                else g.openPreview({ id: f.fileId!, name: f.name, path: f.path ?? "", kind: f.fileKind });
              }}
              title={clickable ? "点击预览" : f.path}
              className={`flex min-w-0 items-center gap-1.5 ${clickable ? "cursor-pointer" : "cursor-default"}`}
            >
              <span>
                {TABLE_KINDS.has(f.fileKind ?? "") ? "📊" : f.fileKind === "image" ? "🖼️" : "📄"}
              </span>
              <span className="max-w-[220px] truncate">{f.name}</span>
            </button>
            {showCodeEntry && (
              <button
                type="button"
                title="在代码面板打开"
                aria-label="在代码面板打开"
                onClick={() => void openChipInCode(g, f)}
                className="flex h-4 w-4 shrink-0 items-center justify-center rounded text-faint transition-colors hover:bg-card hover:text-txt"
              >
                <Code2 size={12} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

type SkillBlock = Extract<Block, { kind: "skill" }>;

/** Slash-skill invocation chips (user bubble, replayed history). The SKILL.md
    body is model scaffolding; the user sees "/name" + their request only. */
export function SkillChips({ skills }: { skills: SkillBlock[] }) {
  if (!skills.length) return null;
  return (
    <div className="mb-1 flex flex-wrap gap-1.5">
      {skills.map((s, i) => (
        <span
          key={`${s.name}-${i}`}
          title={`已调用技能 ${s.name}`}
          className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-2 py-1 text-xs text-violet"
        >
          <Sparkles className="h-3 w-3 shrink-0" />
          <span className="max-w-[220px] truncate font-medium">/{s.name}</span>
        </span>
      ))}
    </div>
  );
}

// Strip "[attached <kind>: <name>]" patterns that the LLM sometimes repeats in its text
// (violating the "don't repeat tool results" instruction). These are shown as ref chips instead.
const ATTACHED_REF_RE = /\[attached\s+\w+:\s*[^\]]+\]/g;

function cleanAgentText(text: string): string {
  return text.replace(ATTACHED_REF_RE, "").replace(/\n{3,}/g, "\n\n").trim();
}

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  pending: "#71717a",
  error: "#ef4444",
};

function StatusGlyph({ status }: { status?: string }) {
  const c = STATUS_COLOR[status || "pending"] || STATUS_COLOR.pending;
  if (status === "done" || status === "ok") return <Check className="h-3.5 w-3.5" style={{ color: c }} />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin" style={{ color: c }} />;
  return <Circle className="h-3.5 w-3.5" style={{ color: c }} />;
}

function StatList({ data }: { data: { title?: string; items?: Array<{ label: string; value?: string; status?: string }> } }) {
  const items = data?.items || [];
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      {data?.title && (
        <div className="mb-2 flex items-center gap-1.5 text-sm font-medium text-txt">
          <Flag className="h-3.5 w-3.5 text-violet" />
          {data.title}
        </div>
      )}
      <div className="space-y-1.5">
        {items.map((it, i) => (
          <div key={i} className="flex items-center gap-2 text-sm">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: STATUS_COLOR[it.status || "pending"] }}
            />
            <span className="text-txt">{it.label}</span>
            {it.value && <span className="text-muted">— {it.value}</span>}
            <span className="ml-auto">
              <StatusGlyph status={it.status} />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---- d3 chart card (render_widget kind="chart") ----
// The model emits a declarative spec ({type, title, x, y, data[], format?});
// d3 only does the math (scales/shapes) and React renders the SVG — d3 never
// touches the DOM here. Colors come from the --chart-N theme tokens
// (globals.css): a fixed CVD-validated categorical order, never cycled.

interface ChartSpec {
  type: "bar" | "line" | "area" | "pie";
  title?: string;
  x: string;
  y: string;
  data: Array<Record<string, unknown>>;
  format?: "number" | "percent" | "currency";
}

const CHART_TYPES = new Set(["bar", "line", "area", "pie"]);
const CHART_W = 560;
const CHART_H = 240;
const CHART_M = { top: 14, right: 14, bottom: 26, left: 46 };
const SERIES = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

/** Defensive parse — null falls back to the JSON-dump widget below. */
function parseChartSpec(raw: unknown): ChartSpec | null {
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Partial<ChartSpec>;
  if (!s.type || !CHART_TYPES.has(s.type)) return null;
  if (typeof s.x !== "string" || !s.x || typeof s.y !== "string" || !s.y) return null;
  if (!Array.isArray(s.data)) return null;
  const xk = s.x;
  const yk = s.y;
  const rows = s.data.filter(
    (d): d is Record<string, unknown> =>
      !!d && typeof d === "object" && d[xk] != null && typeof d[yk] === "number" && isFinite(d[yk] as number),
  );
  if (!rows.length) return null;
  return {
    type: s.type,
    title: typeof s.title === "string" && s.title.trim() ? s.title.trim() : "",
    x: xk,
    y: yk,
    data: rows.slice(0, 30), // hard cap; prompt asks the model to aggregate
    format: s.format,
  };
}

function makeFormatters(format?: string) {
  if (format === "percent") {
    // Accept both fractions (0.42) and pre-scaled percentages (42).
    const fmt = (v: number) =>
      Math.abs(v) <= 1.5 ? d3.format(".1%")(v) : `${d3.format(",.1f")(v)}%`;
    return { axis: fmt, label: fmt };
  }
  if (format === "currency") {
    return { axis: d3.format("$.2~s"), label: d3.format("$,.2~f") };
  }
  return { axis: d3.format(".2~s"), label: d3.format(",.2~f") };
}

function XYChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  const rows = spec.data;
  const xs = rows.map((d) => String(d[spec.x]));
  const ys = rows.map((d) => Number(d[spec.y]));
  const { axis: fAxis, label: fLabel } = makeFormatters(spec.format);

  const y = d3
    .scaleLinear()
    .domain([Math.min(0, d3.min(ys) ?? 0), d3.max(ys) ?? 1])
    .nice()
    .range([CHART_H - CHART_M.bottom, CHART_M.top]);
  const x = d3
    .scaleBand<string>()
    .domain(xs)
    .range([CHART_M.left, CHART_W - CHART_M.right])
    .paddingInner(0.25)
    .paddingOuter(0.12);
  const cx = (i: number) => (x(xs[i]) ?? 0) + x.bandwidth() / 2;
  const ticks = y.ticks(4);
  const step = Math.max(1, Math.ceil(xs.length / 10)); // thin crowded x labels

  const linePath =
    d3.line<number>().x((_, i) => cx(i)).y((v) => y(v)).curve(d3.curveMonotoneX)(ys) ?? "";
  const areaPath =
    d3
      .area<number>()
      .x((_, i) => cx(i))
      .y0(y(Math.max(0, y.domain()[0])))
      .y1((v) => y(v))
      .curve(d3.curveMonotoneX)(ys) ?? "";

  return (
    <>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-auto w-full"
        role="img"
        aria-label={spec.title || "chart"}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={CHART_M.left}
              x2={CHART_W - CHART_M.right}
              y1={y(t)}
              y2={y(t)}
              style={{ stroke: "rgb(var(--line))", strokeDasharray: "2 3" }}
            />
            <text
              x={CHART_M.left - 6}
              y={y(t)}
              dy="0.32em"
              textAnchor="end"
              fontSize={10}
              style={{ fill: "rgb(var(--muted))" }}
            >
              {fAxis(t)}
            </text>
          </g>
        ))}
        {spec.type === "bar" &&
          rows.map((_, i) => (
            <rect
              key={i}
              x={x(xs[i])}
              y={y(Math.max(0, ys[i]))}
              width={x.bandwidth()}
              height={Math.max(1, Math.abs(y(0) - y(ys[i])))}
              rx={3}
              style={{
                fill: SERIES[0],
                opacity: hover === null || hover === i ? 1 : 0.4,
                transition: "opacity 120ms",
              }}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
        {spec.type === "area" && <path d={areaPath} style={{ fill: SERIES[0], opacity: 0.18 }} />}
        {(spec.type === "line" || spec.type === "area") && (
          <>
            <path d={linePath} fill="none" strokeWidth={2} style={{ stroke: SERIES[0] }} />
            {ys.map((v, i) => (
              <g key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                <circle cx={cx(i)} cy={y(v)} r={9} fill="transparent" />
                <circle
                  cx={cx(i)}
                  cy={y(v)}
                  r={hover === i ? 4 : 2.5}
                  style={{
                    fill: SERIES[0],
                    stroke: "rgb(var(--base))",
                    strokeWidth: hover === i ? 1.5 : 0,
                  }}
                />
              </g>
            ))}
          </>
        )}
        {xs.map((v, i) =>
          i % step === 0 ? (
            <text
              key={i}
              x={cx(i)}
              y={CHART_H - 8}
              textAnchor="middle"
              fontSize={10}
              style={{ fill: "rgb(var(--muted))" }}
            >
              {v.length > 9 ? `${v.slice(0, 8)}…` : v}
            </text>
          ) : null,
        )}
      </svg>
      {hover !== null && (
        <div className="mt-1 text-xs text-muted">
          {xs[hover]} · <span className="font-semibold text-txt">{fLabel(ys[hover])}</span>
        </div>
      )}
    </>
  );
}

function PieChart({ spec }: { spec: ChartSpec }) {
  const [hover, setHover] = useState<number | null>(null);
  // Trust the model's aggregation — do not re-fold into Other (that hid
  // later render_widget calls that only expanded the tail).
  const rows = spec.data;
  const vals = rows.map((d) => Math.max(0, Number(d[spec.y])));
  const total = d3.sum(vals) || 1;
  const arcs = d3.pie<number>().sort(null)(vals);
  const R = CHART_H / 2 - 10;
  const cx = CHART_W / 2;
  const cy = CHART_H / 2;
  const mkArc = (r: number) =>
    d3.arc<d3.PieArcDatum<number>>().innerRadius(0).outerRadius(r).cornerRadius(2);
  const pct = d3.format(".0%");
  const { axis: fAxis, label: fLabel } = makeFormatters(spec.format);

  return (
    <>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="h-auto w-full"
        role="img"
        aria-label={spec.title || "chart"}
      >
        <g transform={`translate(${cx},${cy})`}>
          {arcs.map((a, i) => (
            <path
              key={i}
              d={(hover === i ? mkArc(R * 0.76) : mkArc(R * 0.72))(a) ?? ""}
              style={{
                fill: SERIES[i % SERIES.length],
                stroke: "rgb(var(--base))",
                strokeWidth: 2,
                opacity: hover === null || hover === i ? 1 : 0.45,
                transition: "opacity 120ms",
              }}
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            />
          ))}
          {arcs.map((a, i) => {
            const frac = vals[i] / total;
            if (frac < 0.08) return null; // direct labels only where they fit
            const [lx, ly] = mkArc(R * 0.48).centroid(a);
            return (
              <text
                key={`t${i}`}
                x={lx}
                y={ly}
                textAnchor="middle"
                dy="0.32em"
                fontSize={10}
                pointerEvents="none"
                style={{ fill: "rgb(var(--txt))" }}
              >
                {fAxis(vals[i])}
              </text>
            );
          })}
        </g>
      </svg>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
        {rows.map((r, i) => (
          <span
            key={i}
            className="flex cursor-default items-center gap-1.5 text-xs text-muted"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: SERIES[i % SERIES.length] }}
            />
            <span className={hover === i ? "text-txt" : ""}>{String(r[spec.x])}</span>
            <span className="text-faint">
              {fLabel(vals[i])} · {pct(vals[i] / total)}
            </span>
          </span>
        ))}
      </div>
    </>
  );
}

function ChartBlock({ spec }: { spec: ChartSpec }) {
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      {spec.title && (
        <div className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-txt">
          <BarChart3 className="h-3.5 w-3.5 text-violet" />
          {spec.title}
        </div>
      )}
      {spec.type === "pie" ? <PieChart spec={spec} /> : <XYChart spec={spec} />}
    </div>
  );
}

function WidgetBlock({ kind, data }: { kind: string; data: unknown }) {
  if (kind === "stat_list" && data && typeof data === "object") {
    return <StatList data={data as Parameters<typeof StatList>[0]["data"]} />;
  }
  if (kind === "chart") {
    const spec = parseChartSpec(data);
    if (spec) return <ChartBlock spec={spec} />;
    // invalid spec -> fall through to the JSON dump below (debuggable)
  }
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div className="mb-1 text-xs font-medium text-violet">widget · {kind}</div>
      <pre className="whitespace-pre-wrap text-xs text-muted">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

function WorkflowBlock({ run }: { run: WorkflowRun }) {
  const router = useRouter();
  const done = run.steps.filter((s) => s.status === "done").length;
  const total = run.steps.length;
  return (
    <div className="my-2 rounded-lg border border-line bg-base/50 p-3">
      <div
        onClick={() => {
          // Deep-link to THIS run (same fix as RunBlocks). sessionStorage
          // carries the target past the App-Router mount/URL-commit race —
          // see the note in RunBlocks.
          const h = `#wf=${run.workflow_id}&tab=run&run=${run.id}`;
          try {
            sessionStorage.setItem("ginno:studio-deeplink", h);
          } catch {
            /* ignore */
          }
          router.push(`/workflows${h}`);
        }}
        title="打开工作流详情"
        className="mb-2 flex cursor-pointer items-center gap-1.5 text-sm font-medium text-txt hover:text-violet"
      >
        <Workflow className="h-3.5 w-3.5 text-violet" />
        {run.name || "Workflow"}
        <span className="ml-auto text-xs font-normal text-faint">
          {run.status} · {done}/{total}
        </span>
      </div>
      <div className="space-y-1">
        {run.steps.map((s) => (
          <div key={s.id} className="flex items-center gap-2 text-xs">
            <StatusGlyph status={s.status} />
            <span className={s.status === "done" ? "text-muted line-through" : "text-txt"}>
              {s.title}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RefChip({ refKind, name }: { refKind: string; name: string }) {
  const Ic = refKind === "workflow" ? Workflow : refKind === "link" ? Link2 : FileText;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-card px-2.5 py-1 text-xs text-muted">
      <Ic className="h-3.5 w-3.5 text-violet" />
      {refKind === "workflow" ? "Workflow: " : ""}
      {name}
    </span>
  );
}

// Every tool output is collapsible. Long outputs (over these thresholds)
// default to a compact header row and expand into a capped, internally
// scrolling box; short outputs default to expanded.
const LONG_OUTPUT_LINES = 12;
const LONG_OUTPUT_CHARS = 600;

function ToolBlock({ name, content, pending, argsPreview }: { name: string; content: string; pending: boolean; argsPreview?: string }) {
  // null = user hasn't toggled yet → default depends on output length
  // (re-evaluated once content arrives, so pending→done stays correct).
  const [open, setOpen] = useState<boolean | null>(null);
  const label = toolLabel(name);
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
  const isLong = lineCount > LONG_OUTPUT_LINES || content.length > LONG_OUTPUT_CHARS;
  const expanded = open ?? !isLong;
  return (
    <div className="my-1.5 overflow-hidden rounded-md border border-line bg-base/40 font-mono text-xs">
      <button
        onClick={() => setOpen(!expanded)}
        className="flex w-full cursor-pointer items-center gap-1.5 px-2.5 py-1.5 text-left transition-colors hover:bg-card2/50"
        title={`${name} — ${expanded ? "收起" : "展开完整输出"}`}
      >
        <ChevronRight
          className={`h-3 w-3 shrink-0 text-faint transition-transform ${expanded ? "rotate-90" : ""}`}
        />
        <span className="truncate text-faint" title={argsPreview || undefined}>
          tool · <span className="text-muted">{label}</span>
          {argsPreview && <span> · {argsPreview}</span>}
        </span>
        <span className="shrink-0 text-green">✓</span>
        <span className="ml-auto shrink-0 text-[10px] text-faint">
          {lineCount} 行 · {content.length} 字符
        </span>
      </button>
      {expanded && (
        <div className={`border-t border-line/60 ${isLong ? "max-h-80 overflow-y-auto" : ""}`}>
          <pre className="overflow-x-auto whitespace-pre-wrap px-2.5 py-1.5 text-faint">{content}</pre>
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
function ThinkingBlock({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(true);
  const wasLive = useRef(live);
  const scrollRef = useRef<HTMLDivElement>(null);
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
        title={open ? "收起" : "展开思考过程"}
      >
        <Sparkles className={`h-3.5 w-3.5 shrink-0 text-violet ${live ? "animate-pulse" : ""}`} />
        <span className="text-xs font-medium text-violet">
          {live ? "思考中…" : "已深度思考"}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px] text-faint">
          {!live && <span>{text.length} 字</span>}
          <ChevronDown className={`h-3 w-3 transition-transform ${open ? "" : "-rotate-90"}`} />
        </span>
      </button>
      {open && (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="max-h-60 overflow-y-auto border-t border-violet/15 px-3 py-2"
        >
          <div className="whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">{text}</div>
        </div>
      )}
    </div>
  );
}

/** Fullscreen image viewer: ESC / backdrop click closes, ←/→ paginate. */
export function Lightbox({
  urls,
  index,
  onClose,
  onNav,
}: {
  urls: string[];
  index: number;
  onClose: () => void;
  onNav: (i: number) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && urls.length > 1) onNav((index + 1) % urls.length);
      else if (e.key === "ArrowLeft" && urls.length > 1)
        onNav((index - 1 + urls.length) % urls.length);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, urls.length, onClose, onNav]);
  return (
    <div
      className="lightbox-in fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-6"
      onClick={onClose}
      role="dialog"
      aria-label="图片预览"
    >
      <div className="absolute right-4 top-4 flex items-center gap-3 text-xs text-white/70">
        {urls.length > 1 && (
          <span className="rounded-md bg-white/10 px-2 py-0.5">
            {index + 1} / {urls.length}
          </span>
        )}
        <button
          onClick={onClose}
          aria-label="关闭"
          className="rounded-md p-1.5 transition-colors hover:bg-white/10 hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      {urls.length > 1 && (
        <>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNav((index - 1 + urls.length) % urls.length);
            }}
            aria-label="上一张"
            className="absolute left-3 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
          >
            <ChevronLeft className="h-5 w-5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onNav((index + 1) % urls.length);
            }}
            aria-label="下一张"
            className="absolute right-3 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
          >
            <ChevronRight className="h-5 w-5" />
          </button>
        </>
      )}
      <img
        src={urls[index]}
        alt=""
        onClick={(e) => e.stopPropagation()}
        className="lightbox-img max-h-[85vh] max-w-[90vw] rounded-lg object-contain shadow-2xl"
      />
    </div>
  );
}

/** Thumbnail strip for one or more images; click opens the lightbox. */
export function ImageGallery({ urls }: { urls: string[] }) {
  const [lb, setLb] = useState<number | null>(null);
  if (!urls.length) return null;
  const single = urls.length === 1;
  return (
    <>
      <div className="my-2 flex flex-wrap gap-2">
        {urls.map((u, i) => (
          <button
            key={i}
            onClick={() => setLb(i)}
            className="group relative overflow-hidden rounded-lg border border-line transition-colors hover:border-line2"
            title="点击预览"
          >
            <img
              src={u}
              alt=""
              className={`object-cover transition-transform duration-200 group-hover:scale-[1.03] ${
                single ? "max-h-56 max-w-full" : "h-24 w-24"
              }`}
            />
          </button>
        ))}
      </div>
      {lb !== null && (
        <Lightbox urls={urls} index={lb} onClose={() => setLb(null)} onNav={setLb} />
      )}
    </>
  );
}

/** Blocks rendered INSIDE the assistant card (everything except refs).
 * onAnswerQuestion/questionLive thread ChatStream's ask_user resume channel
 * down to a pending question card; without them the card renders read-only. */
export function InnerBlocks({
  blocks,
  streaming,
  onAnswerQuestion,
  questionLive,
}: {
  blocks: Block[];
  streaming?: boolean;
  onAnswerQuestion?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
  questionLive?: boolean;
}) {
  const out: React.ReactNode[] = [];
  let i = 0;
  let key = 0;
  // Resolve `web|sN` citation ids against this bubble's web_search results.
  const refMap = webRefMap(blocks);
  while (i < blocks.length) {
    const b = blocks[i];
    const last = i === blocks.length - 1;
    if (b.kind === "image") {
      // Group consecutive images into one gallery.
      const urls: string[] = [];
      while (i < blocks.length && blocks[i].kind === "image") {
        urls.push(imageUrl(blocks[i] as Extract<Block, { kind: "image" }>));
        i++;
      }
      out.push(<ImageGallery key={key++} urls={urls} />);
      continue;
    }
    if (b.kind === "text") {
      // Citation framework: fold a trailing <ginno_citations> block into a
      // SourcesBlock; while streaming, mask the in-flight (unclosed) block.
      const cited = parseSources(b.text);
      // Strip UNCONDITIONALLY: an empty block (or one whose entries all fail
      // validation) parses to zero items but is still machine metadata — with
      // a conditional strip the raw tags leak into the bubble
      // (2026-08-21: turn b1463216 emitted an empty block).
      let text = stripSources(b.text);
      if (streaming && last && !cited.length) text = maskPartialSources(text);
      out.push(
        <div key={key++}>
          <Markdown text={cleanAgentText(text)} />
          {streaming && last && (
            <span className="ml-0.5 inline-block h-3.5 w-1.5 translate-y-0.5 animate-pulse bg-violet" />
          )}
          {cited.length > 0 && <SourcesBlock items={resolveSourceRefs(cited, refMap)} />}
        </div>,
      );
    } else if (b.kind === "sources") {
      out.push(<SourcesBlock key={key++} items={resolveSourceRefs(b.items, refMap)} />);
    } else if (b.kind === "widget") {
      out.push(
        <WidgetBlock key={b.renderId || `w${key++}`} kind={b.widgetKind} data={b.data} />,
      );
    } else if (b.kind === "workflow") {
      out.push(<WorkflowBlock key={key++} run={b.run} />);
    } else if (b.kind === "tool") {
      out.push(<ToolBlock key={key++} name={b.name} content={b.content} pending={b.pending} argsPreview={b.argsPreview} />);
    } else if (b.kind === "question") {
      out.push(
        <AskUserCard
          key={b.id || `q${key++}`}
          block={b}
          live={questionLive}
          onAnswer={onAnswerQuestion}
        />,
      );
    } else if (b.kind === "thinking") {
      out.push(<ThinkingBlock key={key++} text={b.text} live={!!streaming && last} />);
    } else if (b.kind === "file") {
      out.push(<FileChips key={key++} files={[b]} />);
    } else if (b.kind === "steer") {
      out.push(<SteerBand key={b.steerId || `st${key++}`} block={b} />);
    } else if (b.kind === "subagent_spawn") {
      out.push(<SubagentSpawnCard key={`sa-spawn-${b.sessionId}-${key++}`} block={b} />);
    } else if (b.kind === "subagent_result") {
      out.push(<SubagentResultCard key={`sa-result-${b.sessionId}-${key++}`} block={b} />);
    } else if (b.kind === "subagent_brief") {
      out.push(<SubagentBriefCard key={`sa-brief-${key++}`} block={b} />);
    }
    // refs rendered outside
    i++;
  }
  return <>{out}</>;
}

/** User bubble content: attached images as a gallery, text kept verbatim. */
/** "09:41" from the server's epoch-seconds stamp ("" when absent). */
function steerClock(at?: number): string {
  if (!at) return "";
  const d = new Date(at * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** A mid-turn steered message (docs/steering-design.md §4.2).
 *
 * Full-width band, not a right-aligned bubble: the user typed this INTO a
 * running turn, so it reads as the injection point — sitting between the tool
 * step that was running and the model's continuation — rather than as a turn
 * of its own. Used both inside the assistant bubble (history replay and live
 * absorption) and, on its own, in the "queue bar" above the composer.
 */
/** Resolve a band image to a displayable URL (direct `url`, or a data: URL
 *  rebuilt from a base64 summary). "" when the summary carries no payload. */
function steerImgUrl(img: SteerBandImage): string {
  if (img.url) return img.url;
  if (img.data) return `data:${img.mediaType || img.media_type || "image/png"};base64,${img.data}`;
  return "";
}

export function SteerBand({ block }: { block: Extract<Block, { kind: "steer" }> }) {
  const clock = steerClock(block.injectedAt);
  // Deliberately NOT the thinking block's shape or colour: that block is violet
  // with a header ROW of its own (blocks.tsx ThinkingBlock), and an early
  // version of this band copied both — it read as a second thinking panel and
  // as two lines of chrome for one line of text. Here the label is an inline
  // chip on the same row as the words, and the accent is orange: unused
  // elsewhere in the chat chrome, so it collides with nothing (violet =
  // thinking/agent, blue = web citations, green = success, yellow = needs your
  // attention, red = stop/error).
  // Attachment chips are capped at MAX_VISIBLE with a "+N" tail: the band is a
  // narrow single-line layout (design §4.2), so a long attachment list must
  // truncate gracefully rather than blow the row out.
  const imgs = block.images ?? [];
  const files = block.files ?? [];
  const total = imgs.length + files.length;
  const MAX_VISIBLE = 3;
  const shownImgs = imgs.slice(0, MAX_VISIBLE);
  const shownFiles = files.slice(0, Math.max(0, MAX_VISIBLE - shownImgs.length));
  const overflow = total - shownImgs.length - shownFiles.length;
  return (
    <div className="my-1 flex items-start gap-2 rounded-r-md border-l-2 border-orange/70 bg-orange/[0.07] py-1 pl-2 pr-2">
      <span className="mt-[3px] flex shrink-0 items-center gap-1 rounded bg-orange/15 px-1 py-[1px] text-[10px] font-medium leading-none text-orange">
        <RotateCw className="h-2.5 w-2.5" aria-hidden />
        运行中注入{clock ? ` · ${clock}` : ""}
      </span>
      <div className="min-w-0 whitespace-pre-wrap break-words text-[13px] leading-snug text-txt">
        {block.text}
      </div>
      {total > 0 && (
        <span className="mt-[2px] flex shrink-0 items-center gap-1">
          {shownImgs.map((img, i) => {
            const url = steerImgUrl(img);
            return url ? (
              <img
                key={`i${i}`}
                src={url}
                alt={img.name ?? "图片"}
                title={img.name}
                className="h-4 w-4 shrink-0 rounded border border-orange/40 object-cover"
              />
            ) : (
              <span key={`i${i}`} title={img.name} className="text-[11px] leading-none">
                🖼️
              </span>
            );
          })}
          {shownFiles.map((f, i) => (
            <span
              key={`f${i}`}
              title={f.path ?? f.name}
              className={cn(
                "flex max-w-[96px] items-center gap-0.5 rounded bg-orange/10 px-1 py-px",
                "text-[10px] leading-none text-orange",
              )}
            >
              <span aria-hidden>{TABLE_KINDS.has(f.kind ?? "") ? "📊" : "📄"}</span>
              <span className="truncate">{f.name}</span>
            </span>
          ))}
          {overflow > 0 && (
            <span
              title={`另有 ${overflow} 个附件`}
              className="shrink-0 rounded bg-orange/15 px-1 py-px text-[10px] leading-none text-orange"
            >
              +{overflow}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

// ---- subagent 卡片（subagent-design.md §6.3）--------------------------------
// 主对话里的发起卡片 / 结果卡片。状态永远从 store 的会话元数据实时读——
// subagent.status 事件刷新 store，卡片随之换挡，无需自己的事件订阅。

function subagentElapsed(from?: number): string {
  if (!from) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - from));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** 发起卡片：goal / 约束 / 验收 / 状态脉搏 / 「查看对话」入口。 */
export function SubagentSpawnCard({
  block,
}: {
  block: Extract<Block, { kind: "subagent_spawn" }>;
}) {
  const g = useGinno();
  const router = useRouter();
  const live = g.sessions.find((s) => s.id === block.sessionId);
  const sub = live?.subagent;
  const status = sub?.status ?? "running";
  const meta = subagentStatusMeta(status);
  const active = status === "running" || status === "waiting";
  const openChild = () => {
    // 与通知点击同一条路径：切会话 + 落到最新消息。
    g.setActiveSession(block.sessionId);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: block.sessionId }));
  };
  return (
    <div className="rounded-lg border border-line bg-base/50 px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🤖</span>
        <span className="min-w-0 flex-1 truncate font-medium text-txt" title={sub?.goal || block.goal}>
          {block.title || sub?.goal || block.goal || "子任务"}
        </span>
        <SubagentKindBadges sub={sub} />
        {active && <span className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full" style={{ background: meta.color }} />}
        <span className="shrink-0" title={`状态：${meta.label}`}>
          {meta.glyph} {meta.label}
        </span>
        <button
          onClick={openChild}
          className="shrink-0 rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
        >
          查看对话
        </button>
      </div>
      {(sub?.goal || block.goal) && (
        <div className="mt-1.5 whitespace-pre-wrap break-words leading-relaxed text-muted">
          目标:{sub?.goal || block.goal}
        </div>
      )}
      {(sub?.constraints || block.constraints) && (
        <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
          约束:{sub?.constraints || block.constraints}
        </div>
      )}
      {(sub?.acceptance || block.acceptance) && (
        <div className="mt-1 whitespace-pre-wrap break-words leading-relaxed text-faint">
          验收:{sub?.acceptance || block.acceptance}
        </div>
      )}
      <div className="mt-1.5 flex items-center gap-2 text-[10px] text-faint">
        <span>{block.origin === "user" ? "用户发起" : "主代理发起"}</span>
        {typeof block.depth === "number" && <span>第 {block.depth + 1} 层</span>}
        {block.spawnedAt && <span>已运行 {subagentElapsed(block.spawnedAt)}</span>}
      </div>
    </div>
  );
}

/** 结果卡片：摘要 + 状态 + 验收区 + 确认/纠偏操作（P2）。
 *
 * 确认 = 卡片定稿归档（本地态，localStorage 记忆，见 lib/subagentConfirm）；
 * 有问题 = 两个出口：去子会话纠偏（steering，设计 §5.4）或让主对话处理
 * （invoke/steer 一条引用该结果的消息，主 agent 决定重拆/追问/接手）。
 * 验收判定不在这里解析——runtime 会在注入消息里引导主 agent 逐条对照
 * acceptance 给出一行判定（P2 共享契约 5），卡片只展示 acceptance 原文。 */
export function SubagentResultCard({
  block,
}: {
  block: Extract<Block, { kind: "subagent_result" }>;
}) {
  const g = useGinno();
  const router = useRouter();
  const live = g.sessions.find((s) => s.id === block.sessionId);
  const sub = live?.subagent;
  const status = sub?.status ?? "done";
  const meta = subagentStatusMeta(status);
  const goal = sub?.goal || block.goal || "子任务";
  const [confirmed, setConfirmed] = useState(() => isSubagentConfirmed(block.sessionId));
  const [problemOpen, setProblemOpen] = useState(false);
  const openChild = () => {
    g.setActiveSession(block.sessionId);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: block.sessionId }));
  };
  const confirm = () => {
    markSubagentConfirmed(block.sessionId);
    setConfirmed(true);
  };
  // 出口一：跳进子会话，输入框预填纠偏 steering 提示（运行中走注入，idle 时
  // 是普通消息——预填的只是文本，发送语义由 ChatStream 的既有管线决定）。
  const goToChild = () => {
    openChild();
    window.dispatchEvent(
      new CustomEvent("ginno:prefill-input", {
        detail: {
          sessionId: block.sessionId,
          text: `【纠偏】关于本子任务的目标「${goal}」，结果存在以下问题：`,
        },
      }),
    );
  };
  // 出口二：以一条引用该结果的消息回到当前（主）会话——ChatStream 侧按
  // 会话忙闲决定 invoke 还是 steer（ginno:subagent-escalate 监听）。
  const escalate = () => {
    window.dispatchEvent(
      new CustomEvent("ginno:subagent-escalate", {
        detail: {
          sessionId: block.sessionId,
          goal,
          summary: block.summary || sub?.result_summary || "",
        },
      }),
    );
    setProblemOpen(false);
  };
  return (
    <div className="rounded-lg border border-line bg-card/60 px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🤖</span>
        <span className="min-w-0 flex-1 truncate font-medium text-txt" title={goal}>
          子代理结果 · {goal}
        </span>
        <SubagentKindBadges sub={sub} />
        <span className="shrink-0" title={`状态：${meta.label}`}>
          {meta.glyph} {meta.label}
        </span>
      </div>
      {(block.summary || sub?.result_summary) && (
        <div className="mt-1.5 max-h-60 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed text-muted">
          {block.summary || sub?.result_summary}
        </div>
      )}
      {block.error && (
        <div className="mt-1.5 whitespace-pre-wrap break-words text-red/90">错误:{block.error}</div>
      )}
      {/* 验收区（P2 共享契约 5）：acceptance 非空时展示原文；逐条判定由主 agent
          在汇报里给出（runtime 注入消息已引导），卡片不重复解析。 */}
      {sub?.acceptance && (
        <div className="mt-2 rounded-md border border-violet/25 bg-violet/[0.05] px-2 py-1.5">
          <div className="text-[10px] font-medium text-violet">验收标准</div>
          <div className="mt-0.5 whitespace-pre-wrap break-words leading-relaxed text-muted">
            {sub.acceptance}
          </div>
          <div className="mt-1 text-[10px] text-faint">
            主代理汇报时将逐条对照给出判定（通过 / 有缺口 + 说明）
          </div>
        </div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-line/60 pt-1.5">
        <button
          onClick={openChild}
          className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
        >
          查看完整对话
        </button>
        {confirmed ? (
          <span
            className="rounded-md border border-green/40 bg-green/10 px-1.5 py-0.5 text-[10px] text-green"
            title="已确认该结果，卡片定稿归档"
          >
            ✅ 已确认
          </span>
        ) : (
          <button
            onClick={confirm}
            title="确认该结果，卡片定稿归档"
            className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:border-green/50 hover:text-green"
          >
            ✅ 确认
          </button>
        )}
        {!confirmed && (
          <button
            onClick={() => setProblemOpen((v) => !v)}
            className={`rounded-md border px-1.5 py-0.5 text-[10px] transition-colors ${
              problemOpen
                ? "border-yellow/50 bg-yellow/10 text-yellow"
                : "border-line2 text-muted hover:border-yellow/50 hover:text-yellow"
            }`}
          >
            ↩ 有问题
          </button>
        )}
        {problemOpen && !confirmed && (
          <>
            <button
              onClick={goToChild}
              title="跳转到该子会话，输入框预填纠偏提示（steering / 继续对话）"
              className="rounded-md border border-yellow/40 bg-yellow/10 px-1.5 py-0.5 text-[10px] text-yellow transition-colors hover:bg-yellow/20"
            >
              去子会话纠偏
            </button>
            <button
              onClick={escalate}
              title="在当前会话发一条引用该结果的消息，由主对话决定如何处理"
              className="rounded-md border border-yellow/40 bg-yellow/10 px-1.5 py-0.5 text-[10px] text-yellow transition-colors hover:bg-yellow/20"
            >
              让主对话处理
            </button>
          </>
        )}
        {sub?.acceptance && (
          <span className="ml-auto text-[10px] text-faint">验收判定见主代理汇报</span>
        )}
      </div>
    </div>
  );
}

/** 子代卡片行集（系统行渲染入口）。 */
export function SubagentBlocks({ blocks }: { blocks: SubagentCardBlock[] }) {
  if (!blocks.length) return null;
  return (
    <div className="flex flex-col gap-2">
      {blocks.map((b, i) =>
        b.kind === "subagent_spawn" ? (
          <SubagentSpawnCard key={`${b.sessionId}-${i}`} block={b} />
        ) : b.kind === "subagent_result" ? (
          <SubagentResultCard key={`${b.sessionId}-${i}`} block={b} />
        ) : (
          <SubagentBriefCard key={`sa-brief-row-${i}`} block={b} />
        ),
      )}
    </div>
  );
}

export function UserBlocks({ blocks }: { blocks: Block[] }) {
  const files = blocks.filter((b): b is FileBlock => b.kind === "file");
  const skills = blocks.filter((b): b is SkillBlock => b.kind === "skill");
  const imgs = blocks
    .filter((b): b is Extract<Block, { kind: "image" }> => b.kind === "image")
    .map(imageUrl);
  const texts = blocks
    .filter((b): b is Extract<Block, { kind: "text" }> => b.kind === "text")
    .map((b) => b.text);
  // The user's own request rides the skill block on slash-skill turns.
  const skillTexts = skills.map((s) => s.text ?? "").filter(Boolean);
  const allTexts = [...texts, ...skillTexts];
  return (
    <>
      <FileChips files={files} />
      <SkillChips skills={skills} />
      {imgs.length > 0 && <ImageGallery urls={imgs} />}
      {allTexts.map((t, i) => (
        // break-words: pasted terminal output carries long unbroken paths —
        // without it the transcript overflows the page into horizontal scroll
        <div key={i} className="min-w-0 whitespace-pre-wrap break-words">
          {t}
        </div>
      ))}
    </>
  );
}

/** Ref chips rendered BELOW the card, matching the mock layout. */
export function RefBlocks({ blocks }: { blocks: Block[] }) {
  const refs = blocks.filter((b): b is Extract<Block, { kind: "ref" }> => b.kind === "ref");
  if (!refs.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {refs.map((r, i) => (
        <RefChip key={i} refKind={r.refKind} name={r.name} />
      ))}
    </div>
  );
}

export function hasPendingTool(blocks: Block[]): boolean {
  // A pending question card counts too: it REPLACED the ask_user tool bubble,
  // so without this the working indicator would die the moment the card lands.
  return blocks.some(
    (b) => (b.kind === "tool" && b.pending) || (b.kind === "question" && b.status === "pending"),
  );
}

/** 子会话首条 <ginno_subagent_brief> 消息折出的任务简报卡：goal/约束/验收
 *  一屏可读，报告格式与输出纪律两段附录（notes）收进折叠区。 */
export function SubagentBriefCard({
  block,
}: {
  block: Extract<Block, { kind: "subagent_brief" }>;
}) {
  const [notesOpen, setNotesOpen] = useState(false);
  return (
    <div className="rounded-lg border border-violet/30 bg-violet/[0.04] px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <span className="shrink-0">🧭</span>
        <span className="shrink-0 font-medium text-violet">任务简报</span>
        {block.fork && (
          <span
            className="rounded-md border border-violet/40 bg-violet/10 px-1.5 py-0.5 text-[10px] text-violet"
            title="从父对话分出的并行分支，已继承父对话完整上下文"
          >
            fork
          </span>
        )}
      </div>
      <div className="mt-1.5 whitespace-pre-wrap break-words leading-relaxed text-txt">
        {block.goal}
      </div>
      {block.constraints && (
        <div className="mt-1.5">
          <span className="text-[10px] font-medium text-muted">约束：</span>
          <span className="whitespace-pre-wrap break-words leading-relaxed text-muted">
            {block.constraints}
          </span>
        </div>
      )}
      {block.acceptance && (
        <div className="mt-1">
          <span className="text-[10px] font-medium text-muted">验收标准：</span>
          <span className="whitespace-pre-wrap break-words leading-relaxed text-muted">
            {block.acceptance}
          </span>
        </div>
      )}
      {block.notes && (
        <div className="mt-2 border-t border-line/60 pt-1.5">
          <button
            onClick={() => setNotesOpen((v) => !v)}
            className="text-[10px] text-faint transition-colors hover:text-muted"
          >
            {notesOpen ? "▾" : "▸"} 报告格式与输出纪律
          </button>
          {notesOpen && (
            <div className="mt-1 max-h-48 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed text-faint">
              {block.notes}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
