// streamCore — ChatStream 的纯函数层（自 ChatStream.tsx 机械拆出，行为零变化）。
// 消息气泡/发送载荷类型、turn id 生成、block reducer（applyBlock）与
// 历史/重试载荷重建。无 React 依赖，可单测。

import type { ComposerImage, ComposerFile } from "@/lib/composerAttachments";
import type { ResolvedMention } from "@/components/chat/commandMenu";
import type { Block, QuestionBlock } from "@/components/chat/blocks";

export interface ChatMsg {
id: string;
// "system" = WorldState context chip rows (centered, not a bubble)
role: "user" | "assistant" | "system";
blocks: Block[];
agentId?: string | null;
agentName?: string;
turnId?: string; // per-turn trace UUID (shown on the bubble, greppable in sidecar logs)
// Delivery state — user bubbles only. "sending" = in flight to the sidecar;
// "failed" = never delivered (red ❗, click to retry). Successful delivery
// clears it back to undefined.
status?: "sending" | "failed";
failReason?: string;
// Immutable snapshot of everything the turn carries, kept on the bubble so
// a failed send can be retried or re-edited without losing content.
sendPayload?: SendPayload;
// Assistant turn-error card: the input WAS delivered but the run errored
// (model/provider failure etc.). blocks[0] holds the error text;
// sendPayload carries the originating user turn for the retry button.
error?: boolean;
// Assistant bubble that was streaming when the turn errored — rendered with
// a red border + "回复中断" label so it doesn't look like a normal reply.
failed?: boolean;
// Error cards only: id of the originating user bubble. Retry operates on
// THAT bubble in place — no duplicate message is appended.
sourceMsgId?: string;
}
// The mid-turn steering queue state machine (SteerItem, the per-session queue,
// send / recall / ack handling) now lives in @/lib/steerQueue, shared with the
// floating quick-chat window (steer-queue-shared-brief §3.1).

export interface SendPayload {
text: string;
images: Attachment[];
files: FileAttachment[];
mentions: ResolvedMention[];
agentId: string | null;
}
export const newTurnId = () =>
typeof crypto !== "undefined" && "randomUUID" in crypto
  ? crypto.randomUUID()
  : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
// C+ 方案①：composer chip 的轻量关键词推荐（原型 REC_RULES）。纯客户端
// 启发式，顺序敏感——命中第一条规则即停；只展示「推荐」小标签，绝不自动选中。
const AGENT_REC_RULES: Array<{ agentId: string; kws: string[] }> = [
{ agentId: "research", kws: ["调研", "研究", "查一下", "搜", "资料", "对比", "了解", "竞品"] },
{ agentId: "writer", kws: ["写一篇", "文档", "文章", "总结", "润色", "周报", "邮件", "大纲"] },
{ agentId: "workflow-dev", kws: ["工作流", "workflow", "流水线"] },
{ agentId: "dev", kws: ["代码", "bug", "实现", "修复", "重构", "报错", "函数", "部署"] },
];

export function recommendAgentId(text: string, existing: ReadonlySet<string>): string | null {
const t = text.toLowerCase();
for (const rule of AGENT_REC_RULES) {
  if (!existing.has(rule.agentId)) continue; // 自定义/已删除的 agent 不参与推荐
  if (rule.kws.some((k) => t.includes(k))) return rule.agentId;
}
return null;
}

export interface PermissionPrompt {
tool: string;
args: unknown;
}

export interface VersionPropose {
workflow_id: string;
from_version: number;
diff: string;
rationale: string;
}

let _mid = 0;
export const mid = () => `m${++_mid}`;
// S6: localStorage key for the unsaved summarize draft (24h TTL, see openSummarize).
export const SUMMARIZE_DRAFT_KEY = "ginno-summarize-draft";
const SUMMARIZE_DRAFT_TTL = 24 * 3600 * 1000;

export interface SummarizeDraft {
dsl: Record<string, unknown>;
sourceSessionId?: string;
sourceLabel?: string;
savedAt: number;
}

export function readSummarizeDraft(): SummarizeDraft | null {
try {
  const raw = localStorage.getItem(SUMMARIZE_DRAFT_KEY);
  if (!raw) return null;
  const d = JSON.parse(raw) as SummarizeDraft;
  if (d?.dsl && typeof d.savedAt === "number" && Date.now() - d.savedAt < SUMMARIZE_DRAFT_TTL) {
    return d;
  }
} catch {
  /* corrupted draft */
}
return null;
}

/** One-line summary of a tool call's args for the live-run tool row
 *  (workflow-ux-redesign P1): first string-ish value, truncated to 50 chars. */
export function toolArgsPreview(args: unknown): string {
if (!args || typeof args !== "object") return "";
for (const v of Object.values(args as Record<string, unknown>)) {
  if (typeof v === "string" && v.trim()) {
    const t = v.trim();
    return t.length > 50 ? t.slice(0, 49) + "…" : t;
  }
}
return "";
}
// Composer attachment shapes now live in @/lib/composerAttachments (shared with
// the floating quick-chat window). Aliased so the rest of this file is unchanged.
export type Attachment = ComposerImage;
export type FileAttachment = ComposerFile;

export const TABLE_KINDS = new Set(["spreadsheet", "table"]);

// readImage now lives in @/lib/composerAttachments (shared with the floating
// window). Its 400KB / 1600px / JPEG 0.85 thresholds are unchanged.

// Patterns that indicate a tool returned "no results" — hide these blocks to reduce noise.
// Covers both the builtin tool phrasing ("(no matches)") and the MCP filesystem
// server's phrasing ("No matches found"), so empty search results don't pile up
// as collapsed panels the agent already explains in prose.
const EMPTY_TOOL_RESULT_RE =
/^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

export function isEmptyToolResult(content: string): boolean {
return EMPTY_TOOL_RESULT_RE.test(content);
}
/** ask_user tool.end content → the question card's final state. The receipt
 * is JSON ({ok, skipped, source, option_index, answer, free_text}); a stop
 * while parked persists the literal "(interrupted)" instead. Anything
 * unparseable also reads as skipped — a card left "pending" would keep the
 * session stuck in the running state (hasPendingTool counts it). */
export function foldQuestionResult(q: QuestionBlock, content: string): QuestionBlock {
if (content.trim() !== "(interrupted)") {
  try {
    const r = JSON.parse(content);
    if (r && typeof r === "object") {
      if (r.skipped || r.source === "skipped") return { ...q, status: "skipped" };
      return {
        ...q,
        status: "answered",
        answer: String(r.answer ?? r.free_text ?? ""),
        optionIndex: typeof r.option_index === "number" ? r.option_index : null,
      };
    }
  } catch {
    /* not JSON — falls through to skipped */
  }
}
return { ...q, status: "skipped" };
}

/** Close a dead turn's unfinished blocks so `running` unsticks: pending tool
 * bubbles become "(interrupted)"; pending question cards flip to skipped via
 * their own status field — mirroring what the backend's stop-heal persists
 * ("(interrupted)" tool result), which history replay reads as skipped. */
export function closePendingBlocks(blocks: Block[]): Block[] {
return blocks.map((b) =>
  b.kind === "tool" && b.pending
    ? { ...b, pending: false, content: b.content === "…" ? "(interrupted)" : b.content }
    : b.kind === "question" && b.status === "pending"
      ? { ...b, status: "skipped" as const }
      : b,
);
}

/** Rebuild a retry payload from a history user bubble's blocks — used to
 * re-surface a persisted turn-error card (with working retry) after a reload
 * or route/session switch. Image data URLs round-trip through the checkpoint.
 * Assistant-side kinds (tool/question/widget/…) are ignored BY DESIGN — only
 * user content round-trips into a retry; don't "complete" the switch. */
export function payloadFromBlocks(blocks: Block[], agentId: string | null): SendPayload {
const payload: SendPayload = { text: "", images: [], files: [], mentions: [], agentId };
for (const b of blocks) {
  if (b.kind === "text") {
    payload.text = payload.text ? `${payload.text}\n${b.text}` : b.text;
  } else if (b.kind === "skill") {
    // History-replayed slash-skill turn: resend as the original invocation
    // so the server re-runs the skill substitution.
    const line = b.text ? `/${b.name} ${b.text}` : `/${b.name}`;
    payload.text = payload.text ? `${payload.text}\n${line}` : line;
  } else if (b.kind === "file") {
    payload.files.push({
      id: b.fileId ?? "",
      name: b.name,
      path: b.path ?? "",
      kind: b.fileKind ?? "",
    });
  } else if (b.kind === "image") {
    // Only user-upload data URLs round-trip into a retry payload; generated
    // images (fileId-based) are display-only and skipped here.
    if (!b.url) continue;
    const m = /^data:([^;]+);base64,(.*)$/.exec(b.url);
    if (m) payload.images.push({ data: m[2], mediaType: m[1], preview: b.url, name: "image" });
  }
}
return payload;
}
/** 追加文本/思考增量，与服务端 AIMessageChunk 的「按 content block 索引合并」
 *  对齐：模型会在正文之间零星吐出 thinking 片段（反之亦然），若只在末尾同类才
 *  合并，实时视图会把它们渲染成夹在正文中的独立小卡片，而重放视图（历史）是
 *  合并后的样子——两者不一致（用户反馈 2026-10-01：thinking 与 content 顺序
 *  看起来不对）。合并范围限定在「最近一个非文本/思考块之后」，工具气泡、
 *  steering 带、卡片等仍然是边界。 */
function appendStreamDelta(
  blocks: Block[],
  kind: "text" | "thinking",
  t: string,
): Block[] {
  if (!t) return blocks;
  let start = 0;
  for (let i = blocks.length - 1; i >= 0; i--) {
    const k = blocks[i].kind;
    if (k !== "text" && k !== "thinking") {
      start = i + 1;
      break;
    }
  }
  for (let i = blocks.length - 1; i >= start; i--) {
    if (blocks[i].kind === kind) {
      const next = blocks.slice();
      const cur = next[i] as { kind: "text" | "thinking"; text: string };
      next[i] = { kind, text: cur.text + t };
      return next;
    }
  }
  return [...blocks, { kind, text: t }];
}

export function applyBlock(blocks: Block[], ev: { event: string; [k: string]: unknown }): Block[] {
switch (ev.event) {
  case "token.delta":
    return appendStreamDelta(blocks, "text", (ev.content as string) || "");
  case "thinking.delta":
    return appendStreamDelta(blocks, "thinking", (ev.content as string) || "");
  case "tool.start":
    return [...blocks, { kind: "tool", id: ev.id as string | undefined, name: ev.name as string, content: "…", pending: true }];
  case "tool.args": {
    // Attach the tool call's args preview (e.g. the bash command) to the
    // pending bubble so the user sees WHAT is running, not just the label.
    const id = ev.id as string | undefined;
    const preview = ev.preview as string;
    if (!id || !preview) return blocks;
    let matched = false;
    return blocks.map((b) => {
      if (b.kind !== "tool") return b;
      if (!matched && b.id === id) {
        matched = true;
        return { ...b, argsPreview: preview };
      }
      return b;
    });
  }
  case "tool.end": {
    const id = ev.id as string | undefined;
    const name = ev.name as string | undefined;
    const content = ev.content as string;
    // ask_user: the question card replaced the tool bubble, so the result
    // folds into the card instead. Checked FIRST — the empty-result filter
    // below must never get a chance at a question block (it only drops
    // tool blocks, but the fold has to win before that anyway).
    const qi = id ? blocks.findIndex((b) => b.kind === "question" && b.id === id) : -1;
    if (qi >= 0) {
      const next = blocks.slice();
      next[qi] = foldQuestionResult(next[qi] as QuestionBlock, content);
      return next;
    }
    // Hide tool blocks that returned "no results" to reduce noise
    if (isEmptyToolResult(content)) {
      return blocks.filter((b) => {
        if (b.kind !== "tool") return true;
        const matches = id ? b.id === id : name ? b.name === name : b.pending;
        return !matches;
      });
    }
    let found = false;
    return blocks.map((b) => {
      if (b.kind !== "tool") return b;
      const matches = !found && (id ? b.id === id : name ? b.name === name : b.pending);
      if (matches) {
        found = true;
        return { ...b, content, pending: false };
      }
      return b;
    });
  }
  case "user.question": {
    // ask_user parked the turn: the card REPLACES the pending ask_user tool
    // bubble with the same call id (the user sees "询问中…" first, then the
    // card). Merging by id is what makes a reconnect re-emit a no-op instead
    // of a duplicate card — an existing card may already carry an optimistic
    // answer, so it is kept as-is.
    const id = (ev.id as string | undefined) || undefined;
    if (id && blocks.some((b) => b.kind === "question" && b.id === id)) return blocks;
    const card: QuestionBlock = {
      kind: "question",
      id,
      question: (ev.question as string) || "",
      header: (ev.header as string) || undefined,
      options: (ev.options as string[]) || [],
      allowFreeText: ev.allow_free_text !== false,
      status: "pending",
    };
    const ti = id ? blocks.findIndex((b) => b.kind === "tool" && b.id === id) : -1;
    if (ti >= 0) {
      const next = blocks.slice();
      next[ti] = card;
      return next;
    }
    return [...blocks, card];
  }
  case "widget.emit":
    return [
      ...blocks,
      {
        kind: "widget",
        widgetKind: ev.kind as string,
        data: ev.data,
        renderId: (ev.render_id as string | undefined) || undefined,
      },
    ];
  case "workflow.emit":
    return [...blocks, { kind: "workflow", run: ev.run as import("@/lib/types").WorkflowRun }];
  case "ref.emit":
    return [
      ...blocks,
      { kind: "ref", refKind: ev.kind as string, name: ev.name as string, refId: ev.ref_id as string | undefined },
    ];
  case "image.emit":
    // Code-generated image (bash) surfaced inline; URL resolved from fileId.
    return [
      ...blocks,
      {
        kind: "image",
        fileId: ev.file_id as string,
        name: ev.name as string | undefined,
        mtime: ev.mtime as number | undefined,
      },
    ];
  default:
    return blocks;
}
}

/** 归并连续的子代理发起卡（2026-10-01 空间优化）。
 *
 *  并行委派时 runtime 会为每个子代理发一条 subagent.spawned，客户端各追加一条
 *  system 消息——主对话里就出现 N 张几乎同构的卡片，把主 agent 的内容挤下去。
 *  这里把「连续且只含发起卡」的消息并成一组，由调用方在首条位置渲染一张委派卡
 *  （一行一个子任务），其余消息跳过。
 *
 *  返回 groupAt：首条消息 id → 该组的发起卡块列表；hide：需要跳过渲染的消息 id。
 *  结果卡、简报卡、方案卡不参与归并（它们各自承载内容与操作）。 */
export function foldConsecutiveSpawnCards(
  messages: Array<{ id: string; role: string; blocks: Array<{ kind: string }> }>,
): {
  groupAt: Record<string, Array<Record<string, unknown>>>;
  hide: Set<string>;
} {
  const groupAt: Record<string, Array<Record<string, unknown>>> = {};
  const hide = new Set<string>();
  const isPureSpawn = (m: { role: string; blocks: Array<{ kind: string }> }) =>
    m.role === "system" && m.blocks.length > 0 && m.blocks.every((b) => b.kind === "subagent_spawn");

  let i = 0;
  while (i < messages.length) {
    if (!isPureSpawn(messages[i])) {
      i++;
      continue;
    }
    const rows: Array<Record<string, unknown>> = [];
    const ids: string[] = [];
    let j = i;
    while (j < messages.length && isPureSpawn(messages[j])) {
      rows.push(...(messages[j].blocks as Array<Record<string, unknown>>));
      ids.push(messages[j].id);
      j++;
    }
    groupAt[ids[0]] = rows;
    for (const id of ids.slice(1)) hide.add(id);
    i = j;
  }
  return { groupAt, hide };
}
