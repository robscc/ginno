/**
 * Composer command/mention menu — pure logic (no React).
 *
 * Two trigger modes (see docs/commands-and-mentions-design.md):
 *  - "/" at the start of the input → slash commands (builtins + skills)
 *  - "@" after whitespace/start     → mentions (@artifact/@agent/@workflow/@memory)
 *
 * The server is authoritative for resolution; the menu only improves
 * discoverability and carries the selected mention ids in the invoke payload.
 */

import type { AgentConfig, Artifact, SkillSummary, WorkflowDef } from "@/lib/types";
import { composerCatalog } from "./composerT";

export type MentionKind = "artifact" | "agent" | "workflow" | "memory";

export interface ResolvedMention {
  kind: MentionKind;
  id: string;
  label: string;
}

export interface Trigger {
  kind: "command" | "mention";
  /** Index of the leading "/" or "@" in the input. */
  tokenStart: number;
  /** Raw text typed after the trigger char (used for filtering). */
  query: string;
  /** For mention triggers: the kind prefix once the user typed `kind:`. */
  mentionKind: MentionKind | null;
  /** For mention triggers with a kind prefix: the label substring after `:`. */
  labelQuery: string;
}

export interface MenuItem {
  kind: "command" | "skill" | MentionKind;
  id: string;
  label: string;
  detail?: string;
  group: string;
  /** Token that replaces the trigger text on selection. */
  insert: string;
}

/** Client mirror of the server's builtin registry (discoverability subset).
 *  The server registry stays authoritative for parsing.
 *  命令名/触发词本身不翻译（i18n 硬性约束）；描述文案在 buildMenuItems 时
 *  走 composer 域 catalog 按当前 locale 取词。 */
const BUILTIN_COMMAND_NAMES = ["help", "compact", "subagent"] as const;

/** 内置命令的描述 + mention 类型的分组名/说明，均按当前 locale 从 catalog 取。 */
function mentionKindMeta(kind: MentionKind): { group: string; description: string } {
  const cat = composerCatalog().composer;
  switch (kind) {
    case "artifact":
      return { group: cat.menu.groups.artifacts, description: cat.mentions.artifactDesc };
    case "agent":
      return { group: cat.menu.groups.agents, description: cat.mentions.agentDesc };
    case "workflow":
      return { group: cat.menu.groups.workflows, description: cat.mentions.workflowDesc };
    case "memory":
      return { group: cat.menu.groups.memory, description: cat.mentions.memoryDesc };
  }
}

export const MENTION_KINDS: MentionKind[] = ["artifact", "agent", "workflow", "memory"];

/** Detect an active trigger at the caret. Returns null when no menu applies. */
export function detectTrigger(text: string, caret: number): Trigger | null {
  const before = text.slice(0, caret);
  const after = text.slice(caret);

  // Slash commands: only as the FIRST token — the whole input must be an
  // unfinished "/word" (this keeps "/tmp/foo is the path" menu-free).
  if (/^\/\S*$/.test(before) && after.trim() === "") {
    return {
      kind: "command",
      tokenStart: 0,
      query: before.slice(1),
      mentionKind: null,
      labelQuery: "",
    };
  }

  // Mentions: "@" at the start or after whitespace, optionally followed by
  // "kind:" — e.g. "@art", "@agent:Dev".
  const m = /(^|\s)(@[\w-]*(?::[^\s@]*)?)$/.exec(before);
  if (!m) return null;
  const token = m[2];
  const tokenStart = caret - token.length;
  const body = token.slice(1); // after "@"
  const colon = body.indexOf(":");
  if (colon >= 0) {
    const kindPart = body.slice(0, colon).toLowerCase();
    const mentionKind = (MENTION_KINDS as string[]).includes(kindPart)
      ? (kindPart as MentionKind)
      : null;
    return {
      kind: "mention",
      tokenStart,
      query: body,
      mentionKind,
      labelQuery: mentionKind ? body.slice(colon + 1) : "",
    };
  }
  return { kind: "mention", tokenStart, query: body, mentionKind: null, labelQuery: "" };
}

function matches(haystack: string, needle: string): boolean {
  if (!needle) return true;
  return haystack.toLowerCase().includes(needle.toLowerCase());
}

export interface MenuSources {
  skills: SkillSummary[];
  agents: AgentConfig[];
  workflows: WorkflowDef[];
  artifacts: Artifact[];
}

/** Build the flat, group-ordered item list for the active trigger. */
export function buildMenuItems(trigger: Trigger, src: MenuSources): MenuItem[] {
  // 分组名/说明文案按当前 locale 取词（每次构建菜单时取，切语言后即时生效）
  const cat = composerCatalog().composer;
  if (trigger.kind === "command") {
    const items: MenuItem[] = [];
    const desc: Record<(typeof BUILTIN_COMMAND_NAMES)[number], string> = {
      help: cat.commands.helpDesc,
      compact: cat.commands.compactDesc,
      subagent: cat.commands.subagentDesc,
    };
    for (const name of BUILTIN_COMMAND_NAMES) {
      if (matches(name, trigger.query) || matches(desc[name], trigger.query)) {
        items.push({
          kind: "command",
          id: name,
          label: `/${name}`,
          detail: desc[name],
          group: cat.menu.groups.commands,
          insert: `/${name} `,
        });
      }
    }
    for (const s of src.skills) {
      // model-invocable skills are not slash-addressable (server enforces too)
      if (s.trigger === "model-invocable") continue;
      if (matches(s.name, trigger.query) || matches(s.description, trigger.query)) {
        items.push({
          kind: "skill",
          id: s.name,
          label: `/${s.name}`,
          detail: s.description,
          group: cat.menu.groups.skills,
          insert: `/${s.name} `,
        });
      }
    }
    return items;
  }

  // mention trigger
  const q = trigger.query.toLowerCase();
  const kindFilter = trigger.mentionKind; // null → all kinds
  const labelQ = trigger.labelQuery;
  const items: MenuItem[] = [];
  const push = (kind: MentionKind, id: string, label: string, detail?: string) => {
    if (kindFilter && kind !== kindFilter) return;
    const searchIn = kindFilter ? label : `${kind}:${label} ${detail ?? ""}`;
    const needle = kindFilter ? labelQ : q;
    if (!matches(searchIn, needle)) return;
    const meta = mentionKindMeta(kind);
    items.push({
      kind,
      id,
      label,
      detail: detail || meta.description,
      group: meta.group,
      insert: `@${kind}:${label} `,
    });
  };

  for (const a of src.artifacts) push("artifact", a.id, a.name, a.kind);
  for (const a of src.agents) push("agent", a.id, a.name);
  for (const w of src.workflows) push("workflow", w.id, w.name, w.description);
  push("memory", "global", "MEMORY.md", mentionKindMeta("memory").description);
  return items;
}

export interface SelectionResult {
  text: string;
  caret: number;
  mention?: ResolvedMention;
}

/** Replace the trigger token at the caret with the item's insert text. */
export function applySelection(
  input: string,
  caret: number,
  trigger: Trigger,
  item: MenuItem,
): SelectionResult {
  const before = input.slice(0, trigger.tokenStart);
  const after = input.slice(caret);
  const text = before + item.insert + after;
  const newCaret = before.length + item.insert.length;
  const mention: ResolvedMention | undefined =
    item.kind === "skill" || item.kind === "command"
      ? undefined
      : { kind: item.kind as MentionKind, id: item.id, label: item.label };
  return { text, caret: newCaret, mention };
}

/** Drop resolved mentions whose `@kind:label` token no longer appears. */
export function pruneMentions(mentions: ResolvedMention[], text: string): ResolvedMention[] {
  return mentions.filter((m) => text.includes(`@${m.kind}:${m.label}`));
}

/** Dedupe by kind:id (send-time safety net). */
export function dedupeMentions(mentions: ResolvedMention[]): ResolvedMention[] {
  const seen = new Set<string>();
  const out: ResolvedMention[] = [];
  for (const m of mentions) {
    const k = `${m.kind}:${m.id}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(m);
  }
  return out;
}
