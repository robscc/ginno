import { parseShellOutput } from "@/lib/toolOutput";
import type { Block } from "./types";

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

function isProcess(block: Block, interactive: ReadonlySet<string>): boolean {
  if (block.kind === "thinking" || block.kind === "reasoning_summary") return true;
  return block.kind === "tool" && !standaloneTools.has(block.name) && !interactive.has(block.name)
    && !toolFailed(block.content, block.failed) && !toolInterrupted(block.content);
}

export function processGroups(blocks: readonly Block[], interactive: ReadonlySet<string>): number[][] {
  const groups: number[][] = [];
  let group: number[] | undefined;
  blocks.forEach((block, index) => {
    if (block.kind === "llm_call" || block.kind === "turn_meta" || block.kind === "devswap_result") return;
    if (!isProcess(block, interactive)) {
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
  for (const block of blocks) {
    if (block.kind === "tool") {
      tools++;
      if (block.pending) pending.push(block);
      else completed[toolCategory(block.name)]++;
    } else if (block.kind === "reasoning_summary" && block.receiving) receiving = true;
  }
  const count = tools - pending.length;
  const thinking = streaming && (receiving || (tail && blocks.at(-1)?.kind === "thinking"));
  return { completed, pending, count, thinking, compact: blocks.length >= 3 || tools >= 2 };
}
