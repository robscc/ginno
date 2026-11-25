import { parseShellOutput } from "@/lib/toolOutput";
import type { Block } from "./types";
import { isReportBlock, reportAttention, type ReportAttention } from "./reportFold";

/** Plan progress stamped on a plan note by reportFold.foldReportRows. */
export type PlanProgress = NonNullable<Extract<Block, { kind: "plan_update" }>["progress"]>;

type Tool = Extract<Block, { kind: "tool" }>;
const standaloneTools = new Set([
  "ask_user", "ask_users", "browser_handoff", "bg_run", "spawn_subagent", "delegate_agent",
]);

export function toolFailed(content: string, failed?: boolean): boolean {
  return failed === true || /^\[(?:error\]|blocked:)/i.test(content)
    || (/^\[(?:exit -?\d+|timeout after [^\]]+)\]\n/.test(content) && parseShellOutput(content)?.ok === false);
}

export function toolInterrupted(content: string): boolean {
  return content.trim() === "(interrupted)";
}

export function processFileName(tool: Tool): string | undefined {
  if (!/^(read_file|edit_file|write_file)$/.test(tool.name)) return undefined;
  const name = tool.argsPreview?.split(/[\\/]/).at(-1);
  // Incomplete previews and arbitrary tool arguments stay in the detail row.
  return name && /^[\p{L}\p{N}_. -]{1,64}\.[\p{L}\p{N}]{1,12}$/u.test(name) ? name : undefined;
}

function isProcess(block: Block, interactive: ReadonlySet<string>, attention?: ReportAttention): boolean {
  if (isReportBlock(block)) {
    // A report folds unless it needs the user (failed / stopped / waiting):
    // those break the group and stay visible as cards (design A, the one red line).
    return !(attention ? attention(block) : reportAttention(block));
  }
  if (block.kind === "thinking" || block.kind === "reasoning_summary") return true;
  return block.kind === "tool" && !standaloneTools.has(block.name) && !interactive.has(block.name)
    && !toolInterrupted(block.content);
}

export function processGroups(
  blocks: readonly Block[],
  interactive: ReadonlySet<string>,
  attention?: ReportAttention,
): number[][] {
  const groups: number[][] = [];
  let group: number[] | undefined;
  blocks.forEach((block, index) => {
    if (block.kind === "llm_call" || block.kind === "turn_meta" || block.kind === "devswap_result") return;
    if (!isProcess(block, interactive, attention)) {
      group = undefined;
      return;
    }
    if (!group) groups.push(group = []);
    group.push(index);
  });
  return groups;
}

function toolCategory(name: string) {
  if (/^(read_file|glob_files|grep_files|list_files|parse_document)$/.test(name)) return "read";
  if (/^(edit_file|write_file)$/.test(name)) return "edit";
  if (name === "bash") return "command";
  return "other";
}

export function processStatus(blocks: readonly Block[], streaming: boolean, tail: boolean) {
  const completed = { read: 0, edit: 0, command: 0, other: 0 };
  const pending: Tool[] = [];
  let receiving = false;
  let tools = 0;
  let failed = 0;
  let reports = 0;
  let planUpdates = 0;
  let plan: PlanProgress | null = null;
  for (const block of blocks) {
    if (block.kind === "tool") {
      tools++;
      if (block.pending) pending.push(block);
      else if (toolFailed(block.content, block.failed)) failed++;
      else completed[toolCategory(block.name)]++;
    } else if (block.kind === "reasoning_summary" && block.receiving) receiving = true;
    else if (block.kind === "subagent_result") reports++;
    else if (block.kind === "plan_update") {
      planUpdates++;
      plan = block.progress ?? plan;
    }
  }
  const count = tools - pending.length - failed;
  const thinking = streaming && (receiving || (tail && blocks.at(-1)?.kind === "thinking"));
  // A single report folds too: the pile-up this group exists for is one card
  // per finished item, so "only one so far" must not render as a full card.
  return {
    completed, pending, count, failed, thinking,
    compact: failed > 0 || blocks.length >= 3 || tools >= 2 || reports + planUpdates > 0,
    reports, planUpdates, plan,
  };
}

/** Reports that must stay visible and sit right after a fold group (the group
 *  ends at `start` - 1). They are rendered as cards below it; their counts go
 *  into the group's header so nothing hides behind the chevron. */
export function attentionAfter(
  blocks: readonly Block[], start: number, attention: ReportAttention,
): { failed: number; waiting: number } {
  const counts = { failed: 0, waiting: 0 };
  for (let i = start; i < blocks.length; i++) {
    const block = blocks[i];
    if (block.kind === "turn_meta") continue;
    if (!isReportBlock(block)) break;
    const kind = attention(block);
    if (!kind) break;
    counts[kind]++;
  }
  return counts;
}
