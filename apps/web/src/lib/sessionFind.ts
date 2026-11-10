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
  text: string;
  version: unknown;
  kinds: string[];
}

const msgCache = new Map<string, MsgCacheEntry>();

/** §3.2 提取表：每个 block 一段可搜文本（与索引条目一一对应）。 */
export function extractSearchableText(blocks: Array<Record<string, unknown>>): string[] {
  const out: string[] = [];
  for (const b of blocks) {
    switch (b.kind) {
      case "text":
      case "thinking":
      case "steer":
      case "context":
        if (typeof b.text === "string") out.push(b.text);
        break;
      case "tool":
        // pending（content="…"）不搜；工具输出是「报错在哪条消息」的主目标。
        if (b.pending === true) break;
        out.push(
          `${String(b.name ?? "")}\n${String(b.argsPreview ?? "")}\n${String(b.content ?? "")}`,
        );
        break;
      case "question": {
        const q = [
          String(b.question ?? ""),
          ...(Array.isArray(b.options) ? (b.options as string[]).map(String) : []),
          String(b.answer ?? ""),
        ].join("\n");
        out.push(q);
        break;
      }
      case "skill":
        out.push(`/${String(b.name ?? "")} ${String(b.text ?? "")}`);
        break;
      case "subagent_result":
      case "worker_result":
        out.push(`${String(b.goal ?? "")}\n${String(b.summary ?? "")}\n${String(b.text ?? "")}`);
        break;
      case "file":
      case "ref":
        out.push(String(b.name ?? ""));
        break;
      default:
        break; // image/widget/workflow/sources/turn_meta/… 不入索引（§3.2）
    }
  }
  return out;
}

export function buildFindIndex(
  messages: Array<{ id: string; blocks?: Array<Record<string, unknown>> }>,
  liveId: string | null | undefined,
): FindIndexEntry[] {
  const entries: FindIndexEntry[] = [];
  for (const m of messages) {
    const blocks = m.blocks ?? [];
    let cached = msgCache.get(m.id);
    if (!cached || cached.version !== blocks) {
      cached = {
        text: "",
        version: blocks,
        kinds: [],
      };
      // 段间用 \u0000 分隔，检索时每段独立 indexOf（start 为段内偏移）。
      const parts = extractSearchableText(blocks);
      cached.text = parts.join("\u0000");
      cached.kinds = blocks.map((b) => String(b.kind));
      msgCache.set(m.id, cached);
    }
    // blockIdx 只数入索引的 block（与 extractSearchableText 的输出序一致）。
    let outIdx = -1;
    let cursor = 0;
    const parts = cached.text.split("\u0000");
    for (let bi = 0; bi < blocks.length; bi++) {
      const seg = extractSearchableText([blocks[bi]]);
      if (seg.length === 0) continue;
      outIdx += 1;
      const text = parts[outIdx] ?? seg[0];
      entries.push({
        msgId: m.id,
        blockIdx: outIdx,
        kind: cached.kinds[bi],
        text,
        streaming: liveId != null && m.id === liveId,
      });
      cursor += text.length + 1;
    }
    void cursor;
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
