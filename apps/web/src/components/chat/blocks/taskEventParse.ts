import type { Block } from "./types";

// Legacy checkpoints use the exact header templates from tasks/inject.py.
const STATUS = {
  finished: "finished",
  "failed to start": "failed_to_start",
  "stopped by user": "stopped",
  timeout: "timeout",
  "interrupted by app quit": "interrupted",
  healthy: "healthy",
  watch: "watch",
  "watch muted": "watch_muted",
} as const;
const statusPattern = Object.keys(STATUS).join("|");
const taskHeader = String.raw`\[task (${statusPattern})\] "([^\r\n]*)" \((t_[0-9a-zA-Z]+)\)([^\r\n]*)`;
const TASK_HEADER_RE = new RegExp(`^${taskHeader}`);
const LEGACY_BATCH_RE = /^([2-9]|[1-9]\d+) background task events arrived:\n\n/;
const LEGACY_BATCH_SEPARATOR = new RegExp(String.raw`\n\n(?=\[task (?:${statusPattern})\] ")`);

function parseTaskEvent(text: string): Extract<Block, { kind: "task_event" }> | undefined {
  const match = TASK_HEADER_RE.exec(text);
  if (!match) return;
  const [, status, title, taskId, rest] = match;
  const ev = STATUS[status as keyof typeof STATUS];
  const exit = / exit (-?\d+)/.exec(rest)?.[1];
  const dur = / · ran (.+)$| after (\d+(?:h|m|s)(?: \d+(?:m|s))?)(?= —|$)/.exec(rest);
  const note = ["healthy", "watch", "watch_muted", "failed_to_start"].includes(ev) ? rest.trim() : "";
  const tail = text.slice(match[0].length).trim();
  const log = [note, tail].filter(Boolean).join("\n");
  return {
    kind: "task_event",
    taskId,
    ev,
    title,
    ...(exit != null ? { exit: Number(exit) } : {}),
    ...(dur ? { dur: dur[1] ?? dur[2] } : {}),
    ...(log ? { log } : {}),
  };
}

export function foldTaskEventBlocks(blocks: Block[]): Block[] {
  return blocks.flatMap((block): Block[] => {
    if (block.kind !== "text") return [block];
    // Keep [lastwords] intact so TranscriptList can render the whole recovery receipt.
    const batch = LEGACY_BATCH_RE.exec(block.text);
    if (batch) {
      const events = block.text.slice(batch[0].length).split(LEGACY_BATCH_SEPARATOR).map(parseTaskEvent);
      if (events.length === Number(batch[1]) && events.every((event) => event !== undefined)) return events;
      return [block];
    }
    const event = parseTaskEvent(block.text);
    return event ? [event] : [block];
  });
}
