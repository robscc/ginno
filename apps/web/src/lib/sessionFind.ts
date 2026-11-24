/**
 * sessionFind — Cmd+F 会话内搜索的纯函数层（docs/design/session-find-bar-design.md §3）。
 *
 * 索引看数据（messages 全量，窗口外天然可搜），高亮看 DOM（findHighlight.ts）。
 * node 环境可测（vitest）。
 */

export interface FindIndexEntry {
  msgId: string;
  blockIdx: number;
  kind: string;
  text: string;
  streaming: boolean;
}

export interface FindHit {
  msgId: string;
  blockIdx: number;
  kind: string;
  start: number;
  len: number;
}

/** 每条消息一个版本缓存条目：blocks 数组引用变才重提取（§3.1）。 */
interface MsgCacheEntry {
  version: unknown;
  entries: Array<Pick<FindIndexEntry, "blockIdx" | "kind" | "text">>;
}

const msgCache = new Map<string, MsgCacheEntry>();

function searchableText(block: Record<string, unknown>): string | null {
  switch (block.kind) {
    case "text":
    case "thinking":
    case "steer":
    case "context":
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
    case "subagent_result":
    case "worker_result":
      return [block.goal, block.summary, block.text].map((value) => String(value ?? "")).join("\n");
    case "file":
    case "ref":
      return String(block.name ?? "");
    default:
      return null;
  }
}

export function extractSearchableText(blocks: Array<Record<string, unknown>>): string[] {
  return blocks.flatMap((block) => {
    const text = searchableText(block);
    return text === null ? [] : [text];
  });
}

export function buildFindIndex(
  messages: Array<{ id: string; blocks?: Array<Record<string, unknown>> }>,
  liveId: string | null | undefined,
): FindIndexEntry[] {
  const entries: FindIndexEntry[] = [];
  for (const message of messages) {
    const blocks = message.blocks ?? [];
    let cached = msgCache.get(message.id);
    if (!cached || cached.version !== blocks) {
      // Keep the original ordinal so the hit still finds its DOM wrapper
      // when earlier process records are folded or excluded from the index.
      const entries = blocks.flatMap((block, blockIdx) => {
        const text = searchableText(block);
        return text === null ? [] : [{ blockIdx, text, kind: String(block.kind) }];
      });
      cached = { version: blocks, entries };
      msgCache.set(message.id, cached);
    }
    for (const entry of cached.entries) {
      entries.push({ ...entry, msgId: message.id, streaming: message.id === liveId });
    }
  }
  return entries;
}

export function searchFindIndex(index: FindIndexEntry[], query: string): FindHit[] {
  const q = query.trim();
  if (!q) return [];
  const needle = q.toLowerCase();
  const hits: FindHit[] = [];
  for (const e of index) {
    const hay = e.text.toLowerCase();
    let from = 0;
    for (;;) {
      const at = hay.indexOf(needle, from);
      if (at < 0) break;
      hits.push({ msgId: e.msgId, blockIdx: e.blockIdx, kind: e.kind, start: at, len: q.length });
      from = at + Math.max(1, needle.length);
    }
  }
  // 会话时间序：messages 顺序即时间序，index 本身有序——hits 自然有序。
  return hits;
}

export function resetFindCache(): void {
  msgCache.clear();
}
