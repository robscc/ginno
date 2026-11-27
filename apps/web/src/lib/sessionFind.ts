import type { Block } from "@/components/chat/blocks/types";
import type { ChatMsg } from "@/components/chat/streamCore";
import type { FoldReportRowsResult } from "@/components/chat/blocks/reportFold";

export const FIND_REVEAL_ROW = "ginno:find-reveal-row";

/** Address in the projected transcript. Hosts can change when history reconciles. */
export interface FindTarget {
  rowKey: string;
  blockIdx: number;
}

export interface FindIndexEntry extends FindTarget {
  /** Identity of the source block, independent of its render host. */
  sourceKey: string;
  text: string;
}

export interface FindHit extends FindTarget {
  sourceKey: string;
  /** Zero-based occurrence of this query within the source block. */
  occurrence: number;
}

type FindMessage = Pick<ChatMsg, "blocks" | "role" | "turnId"> & { id?: string | null };

export function mergeFindHistory(history: ChatMsg[], current: ChatMsg[], liveId: string | null): ChatMsg[] {
  const saved = new Set(history.map((row) => row.id));
  const live = liveId ? current.find((row) => row.id === liveId) : undefined;
  const rows = live && saved.has(live.id) ? history.map((row) => {
    if (row.id !== live.id) return row;
    // A stream can resume in the same bubble that the checkpoint indexed.
    // Take the displayed blocks and restore full bodies to lazy tool previews.
    const tools = new Map(row.blocks.flatMap((block) =>
      block.kind === "tool" && block.id ? [[block.id, block] as const] : []));
    const blocks = live.blocks.map((block) => {
      if (block.kind !== "tool" || !block.detail || !block.id) return block;
      const full = tools.get(block.id);
      return full?.name === block.name ? { ...block, content: full.content, diff: full.diff ?? block.diff } : block;
    });
    return { ...live, blocks };
  }) : history;
  // An addressed history row missing from the current snapshot was removed
  // by a checkpoint rewrite. The current transcript can still lag behind it.
  return [...rows, ...current.filter((row) => !saved.has(row.id) && (!row.history || row.id === liveId))];
}

function nextOrdinal(counts: Map<string, number>, key: string): number {
  const ordinal = counts.get(key) ?? 0;
  counts.set(key, ordinal + 1);
  return ordinal;
}

export function transcriptRowKeys(messages: readonly FindMessage[]): string[] {
  const counts = new Map<string, number>();
  return messages.map((message) => {
    const key = message.id ? JSON.stringify(["message", message.id])
      : JSON.stringify(["legacy", message.role, message.turnId, message.blocks[0]?.kind]);
    return `${key}:${nextOrdinal(counts, key)}`;
  });
}

function sourceBlockDiscriminator(block: Block): unknown[] {
  // Reports can be separate live rows and replay at the head of a different
  // message. Their envelope fields survive both paths (and null history ids).
  if (block.kind === "subagent_result") return [block.kind, block.sessionId];
  if (block.kind === "plan_update") return [block.kind, block.planId, block.text];
  if (block.kind === "worker_result" && block.msgId) return [block.kind, block.msgId];
  return [];
}

const textCache = new WeakMap<Block, string | null>();

function extractBlockText(block: Block): string | null {
  switch (block.kind) {
    case "text":
    case "thinking":
    case "steer":
      return typeof block.text === "string" ? block.text : null;
    case "reasoning_summary": {
      if (!Array.isArray(block.parts)) return null;
      return block.parts.map((part) => String(part?.text ?? "")).join("\n");
    }
    case "tool": {
      if (block.pending === true) return null;
      const diff = block.diff as { text?: string; path?: string } | undefined;
      const fields = [block.name, block.argsPreview, block.content];
      if (diff) fields.push(diff.path, diff.text);
      return fields.map((value) => String(value ?? "")).join("\n");
    }
    case "question":
      return [block.question, ...(Array.isArray(block.options) ? block.options : []), block.answer]
        .map((value) => String(value ?? "")).join("\n");
    case "skill":
      return `/${String(block.name ?? "")} ${String(block.text ?? "")}`;
    case "worker_result":
      return [block.goal, block.summary].map((value) => String(value ?? "")).join("\n");
    case "file":
    case "ref":
      return String(block.name ?? "");
    default:
      return null;
  }
}

export function searchableBlockText(block: Block): string | null {
  if (!textCache.has(block)) textCache.set(block, extractBlockText(block));
  return textCache.get(block) ?? null;
}

/** Use the same projection the transcript renders, in presentation order. */
export function buildFindIndex(
  messages: readonly FindMessage[],
  projection: { folded: Pick<FoldReportRowsResult<ChatMsg>, "rows" | "origins">; rowKeys: string[] },
  displayText: (block: Block, row: ChatMsg) => string | null,
  originalMessages: readonly FindMessage[] = messages,
): FindIndexEntry[] {
  const { folded, rowKeys } = projection;
  const counts = new Map<string, number>();
  const originalNotices = originalMessages === messages ? null : new Map(
    originalMessages.filter((message) => message.role === "system").map((message) => [message.id, message.blocks]),
  );
  const keys = messages.map((message, row) => message.blocks.map((block, index) => {
    // Dismissing one chip can change the visible ordinals of its siblings.
    if (block.kind === "context") {
      const originalIndex = originalNotices?.get(message.id)?.indexOf(block) ?? index;
      return JSON.stringify([rowKeys[row], block.kind, originalIndex]);
    }
    const semantic = sourceBlockDiscriminator(block);
    const recordId = "id" in block ? block.id : "requestId" in block ? block.requestId : undefined;
    const identity = semantic.length > 0 ? semantic : [rowKeys[row], block.kind, recordId];
    const key = JSON.stringify(identity);
    return `${key}:${nextOrdinal(counts, key)}`;
  }));
  return folded.rows.flatMap((row, rowIndex) => row.blocks.flatMap((block, blockIdx) => {
    const text = displayText(block, row);
    if (!text) return [];
    const origin = folded.origins[rowIndex][blockIdx];
    return [{ sourceKey: keys[origin.row][origin.block], rowKey: rowKeys[rowIndex], blockIdx, text }];
  }));
}

/** Keep offsets in the original text even for Unicode case-insensitive matching. */
export function findOccurrences(text: string, query: string): Iterable<RegExpMatchArray> {
  const literal = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return literal ? text.matchAll(new RegExp(literal, "giu")) : [];
}

export function searchFindIndex(index: FindIndexEntry[], query: string): FindHit[] {
  const needle = query.trim();
  if (!needle) return [];
  return index.flatMap(({ text, ...target }) =>
    Array.from(findOccurrences(text, needle), (_, occurrence) => ({ ...target, occurrence })),
  );
}
