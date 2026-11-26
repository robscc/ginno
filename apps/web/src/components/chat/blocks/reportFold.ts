import type { Block } from "./types";
import type { ChatMsg } from "../streamCore";
import { parseDelegation } from "./delegation";
import { parsePlanUpdate } from "./planUpdateParse";

export type ResultBlock = Extract<Block, { kind: "subagent_result" }>;
export type PlanUpdateBlock = Extract<Block, { kind: "plan_update" }>;
export type ReportBlock = ResultBlock | PlanUpdateBlock;

export function isReportBlock(block: Block): block is ReportBlock {
  return block.kind === "subagent_result" || block.kind === "plan_update";
}

/** Why a report must stay visible: the work broke, or someone is waiting on
 *  the user. Drives both the fold boundary and the header's count chips. */
export type AttentionKind = "failed" | "waiting";

export function resultAttention(block: ResultBlock, childStatus?: string): AttentionKind | null {
  if (block.error?.trim()) return "failed";
  if (childStatus === "waiting") return "waiting";
  if (childStatus === "failed" || childStatus === "stopped") return "failed";
  const stop = parseDelegation(block.summary)?.stop;
  return stop === "error" || stop === "timeout" ? "failed" : null;
}

export function reportAttention(block: ReportBlock, childStatus?: string): AttentionKind | null {
  return block.kind === "subagent_result"
    ? resultAttention(block, childStatus)
    : planUpdateNeedsAttention(block) ? "failed" : null;
}

export type ReportAttention = (block: ReportBlock) => AttentionKind | null;

export function reportNeedsAttention(block: ReportBlock, childStatus?: string): boolean {
  return reportAttention(block, childStatus) !== null;
}

/** A result the user must see without opening anything: an error text, a child
 *  that failed / was stopped / is waiting for an answer, or a delegation that
 *  ended in error or timeout. */
export function resultNeedsAttention(block: ResultBlock, childStatus?: string): boolean {
  return resultAttention(block, childStatus) !== null;
}

/** Plan notes are `- [<status>] <item> …` lines; a failed or stopped item must
 *  stay visible (its result card may not exist — a launch failure has no child). */
export function planUpdateNeedsAttention(block: PlanUpdateBlock): boolean {
  return parsePlanUpdate(block.text).items.some(
    (item) => item.status === "failed" || item.status === "stopped",
  );
}


/** A message that carries nothing but reports — live system rows, replayed
 *  result envelopes, and an assistant bubble whose whole content is reports
 *  (a wake turn that produced no prose). */
export function isReportMessage(message: Pick<ChatMsg, "blocks">): boolean {
  return message.blocks.length > 0 && message.blocks.every(isReportBlock);
}

/** Block kinds that travel in an injected system row next to reports. A row
 *  made of these renders through the same folding renderer as an assistant
 *  bubble, so live and replay fold a coalesced envelope identically. Context
 *  chips are left out: their dismiss callback only exists on the old path. */
const ROW_KINDS: ReadonlySet<string> = new Set([
  "subagent_result", "plan_update", "worker_result", "worker_assign", "subagent_brief", "task_event",
]);

export function isReportRow(message: Pick<ChatMsg, "role" | "blocks">): boolean {
  return message.role !== "assistant"
    && message.blocks.some(isReportBlock)
    && message.blocks.every((block) => ROW_KINDS.has(block.kind));
}

function canHost(message: Pick<ChatMsg, "role" | "error" | "teamReply">): boolean {
  return message.role === "assistant" && !message.error && !message.teamReply;
}

function leadingReports(blocks: readonly Block[]): number {
  let lead = 0;
  while (lead < blocks.length && isReportBlock(blocks[lead])) lead++;
  return lead;
}

function sameBlocks(a: readonly Block[], b: readonly Block[]): boolean {
  return a.length === b.length && a.every((block, index) => block === b[index]);
}

export interface BlockOrigin {
  row: number;
  block: number;
}

export interface FoldReportRowsResult<T extends ChatMsg> {
  /** Same length and order as the input: the rows of a run attached elsewhere
   *  end up with no blocks and their indexes are listed in `absorbed` (the
   *  caller renders nothing for them), so slicing, windowing and message
   *  anchors keep addressing the original list. Indexes, not ids: replayed
   *  trailing rows can carry a null id. */
  rows: T[];
  absorbed: Set<number>;
  /** Original coordinates parallel to rows[row].blocks, including cloned plan notes. */
  origins: BlockOrigin[][];
}

/** One render unit: a whole message, or one of the two halves of a message
 *  whose leading reports were detached. */
interface Entry {
  role: ChatMsg["role"];
  blocks: Block[];
  canHost: boolean;
  origins: BlockOrigin[];
  /** True for the detached leading half: its blocks precede the message. */
  leads: boolean;
}

/**
 * Attach each run of reports to the assistant message right before it: the
 * reports then fold into that bubble's trailing 「执行过程」 group together
 * with its own tool steps. A run with no assistant host (start of the
 * transcript, after a user bubble, …) keeps its first row and carries the
 * whole run, rendered as a standalone fold.
 *
 * Replay puts result envelopes at the HEAD of the next assistant bubble
 * (messages_ui.py `pending_bands`); live appends them after the previous one.
 * Leading report blocks are detached and handled as a run that arrived before
 * that bubble, so live and replay fold at the same spot.
 *
 * Pure: both paths hand over the same message shapes, so they fold identically.
 * Plan progress is accumulated from the plan notes seen so far and stamped on
 * each plan block: a fold header reads 「清单 3/8」 as of that point in the
 * timeline, never the plan's current state.
 */
export function foldReportRows<T extends ChatMsg>(
  messages: T[],
  opts: { attention: ReportAttention; planTotal?: (planId: string) => number | null },
): FoldReportRowsResult<T> {
  const entries: Entry[] = [];
  const owner: number[] = [];
  messages.forEach((message, index) => {
    const origins = message.blocks.map((_, block) => ({ row: index, block }));
    const lead = message.role === "assistant" ? leadingReports(message.blocks) : 0;
    if (lead > 0 && lead < message.blocks.length) {
      entries.push({ role: "system", blocks: message.blocks.slice(0, lead), origins: origins.slice(0, lead), canHost: false, leads: true });
      owner.push(index);
      entries.push({ role: "assistant", blocks: message.blocks.slice(lead), origins: origins.slice(lead), canHost: canHost(message), leads: false });
    } else {
      entries.push({ role: message.role, blocks: message.blocks, origins, canHost: canHost(message), leads: false });
    }
    owner.push(index);
  });

  const itemStatus = new Map<string, Map<string, string>>();
  const absorbPlanNotes = (blocks: readonly Block[]) => {
    for (const block of blocks) {
      if (block.kind !== "plan_update") continue;
      const items = itemStatus.get(block.planId) ?? new Map<string, string>();
      for (const item of parsePlanUpdate(block.text).items) items.set(item.id, item.status);
      itemStatus.set(block.planId, items);
    }
  };
  const withProgress = (blocks: Block[]): Block[] => {
    const progressByPlan = new Map<string, NonNullable<PlanUpdateBlock["progress"]>>();
    for (const block of blocks) {
      if (block.kind !== "plan_update" || progressByPlan.has(block.planId)) continue;
      const items = itemStatus.get(block.planId);
      const count = (status: string) => [...(items?.values() ?? [])].filter((s) => s === status).length;
      progressByPlan.set(block.planId, {
        planId: block.planId,
        done: count("done"),
        failed: count("failed"),
        stopped: count("stopped"),
        total: opts.planTotal?.(block.planId) ?? null,
      });
    }
    return blocks.map((block) =>
      block.kind === "plan_update" ? { ...block, progress: progressByPlan.get(block.planId) } : block,
    );
  };

  const isRun = (entry: Entry) => isReportMessage(entry);
  let lastHost = -1;
  for (let i = 0; i < entries.length; i++) {
    const entry = entries[i];
    if (!isRun(entry)) {
      if (entry.canHost) lastHost = i;
      continue;
    }
    let end = i;
    while (end + 1 < entries.length && isRun(entries[end + 1])) end++;
    const runEntries = entries.slice(i, end + 1);
    const run = runEntries.flatMap((e) => e.blocks as ReportBlock[]);
    const runOrigins = runEntries.flatMap((e) => e.origins);
    absorbPlanNotes(run);
    const blocks = withProgress(run);
    // Attached: appended after the host's own content. Standalone: the run's
    // first entry is replaced by the whole run (it already holds its own blocks).
    const host = lastHost === i - 1 ? lastHost : -1;
    const target = entries[host === -1 ? i : host];
    target.blocks = host === -1 ? blocks : [...target.blocks, ...blocks];
    target.origins = host === -1 ? runOrigins : [...target.origins, ...runOrigins];
    for (let k = host === -1 ? i + 1 : i; k <= end; k++) {
      entries[k].blocks = [];
      entries[k].origins = [];
    }
    i = end;
  }

  const outcome: Block[][] = messages.map(() => []);
  const origins: BlockOrigin[][] = messages.map(() => []);
  entries.forEach((entry, k) => {
    const index = owner[k];
    outcome[index] = entry.leads
      ? [...entry.blocks, ...outcome[index]]
      : [...outcome[index], ...entry.blocks];
    origins[index] = entry.leads
      ? [...entry.origins, ...origins[index]]
      : [...origins[index], ...entry.origins];
  });
  const rows = messages.map((message, index) =>
    sameBlocks(outcome[index], message.blocks) ? message : { ...message, blocks: outcome[index] },
  );
  const absorbed = new Set(
    messages.flatMap((m, index) =>
      m.blocks.length > 0 && outcome[index].length === 0 ? [index] : []),
  );
  return { rows, absorbed, origins };
}

/** One-line-ish plain text for a collapsed report: the two-line preview must
 *  read as prose, not as raw Markdown (`###`, backticks, link syntax). Code
 *  blocks collapse to an ellipsis — they are never what a preview is for. */
export function reportPreview(markdown: string, includeCode = false): string {
  return (markdown ?? "")
    .replace(/```([\s\S]*?)```/g, (_, code: string) => includeCode ? code.replace(/^[^\n]*\n/, "") : " … ")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/(\*\*|__|~~)([^\n]+?)\1/g, "$2")
    .replace(/(^|[^\w*])(\*|_)([^\n_]+?)\2(?=\W|$)/g, "$1$3")
    .replace(/^[\s|:-]*-[\s|:-]*$/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}
