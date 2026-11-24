/**
 * Fold a `<ginno_plan_update>` body into its item status changes.
 *
 * The runtime (plans/watcher.py `_note`) writes one `- [<status>] <item> <detail>`
 * line per settled item, and `plan <id> COMPLETED: <n> done, <m> failed` when the
 * plan settles. A detail is the first 200 characters of the subagent's summary,
 * so it can span several lines: everything up to the next item line belongs to
 * the item above it. The model-facing header is scaffolding and is dropped here.
 */

export interface PlanUpdateItem {
  id: string;
  status: string;
  detail: string;
}

export interface ParsedPlanUpdate {
  items: PlanUpdateItem[];
  completed: { planId: string; done: number; failed: number } | null;
  /** Lines that are neither scaffolding nor item notes (older/unknown shapes). */
  notes: string[];
}

const HEADER_RE = /^\[plan update\]\s*/i;
const SCAFFOLD_RE = /^task list status changed:?\s*$/i;
const ITEM_RE = /^-\s*\[([a-z_]+)\]\s+(\S+)(?:\s+([\s\S]*))?$/i;
const COMPLETED_RE = /^plan\s+(\S*)\s+COMPLETED:\s*(\d+)\s+done,\s*(\d+)\s+failed\s*$/i;

export function parsePlanUpdate(text: string): ParsedPlanUpdate {
  const parsed: ParsedPlanUpdate = { items: [], completed: null, notes: [] };
  let current: PlanUpdateItem | null = null;
  for (const raw of (text ?? "").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      if (current) current.detail += "\n";
      continue;
    }
    const trimmed = HEADER_RE.test(line.trim()) ? line.trim().replace(HEADER_RE, "") : line.trim();
    const item = ITEM_RE.exec(trimmed);
    if (item) {
      current = { id: item[2], status: item[1].toLowerCase(), detail: (item[3] ?? "").trim() };
      parsed.items.push(current);
      continue;
    }
    const settled = COMPLETED_RE.exec(trimmed);
    if (settled) {
      current = null;
      parsed.completed = { planId: settled[1], done: Number(settled[2]), failed: Number(settled[3]) };
      continue;
    }
    if (SCAFFOLD_RE.test(trimmed)) {
      current = null;
      continue;
    }
    if (current) current.detail += `\n${trimmed}`;
    else parsed.notes.push(trimmed);
  }
  for (const it of parsed.items) it.detail = it.detail.trim();
  return parsed;
}
