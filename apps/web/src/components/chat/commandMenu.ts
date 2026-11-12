/**
 * Composer command/mention menu — pure logic (no React).
 *
 * Two trigger modes (see docs/commands-and-mentions-design.md):
 *  - "/" at the start of the input → slash commands (builtins + skills)
 *  - "@" after whitespace/start     → mentions (@artifact/@agent/@session/@result)
 *
 * The server is authoritative for resolution; the menu only improves
 * discoverability and carries the selected mention ids in the invoke payload.
 */

import type { AgentConfig, Artifact, SkillSummary, WorkflowDef } from "@/lib/types";
import { composerCatalog } from "./composerT";

export type MentionKind = "artifact" | "agent" | "session" | "result";

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
  /** 组头计数徽标分母（limitMenuItems 截断时填在每组首条；渲染层显示 N/M）。 */
  groupTotal?: number;
  /** 禁选（可见不可选）：@ 投递菜单里 blocked 成员——设计稿 ⛔ 不可投递。 */
  disabled?: boolean;
}

/** Client mirror of the server's builtin registry (discoverability subset).
 *  The server registry stays authoritative for parsing.
 *  命令名/触发词本身不翻译（i18n 硬性约束）；描述文案在 buildMenuItems 时
 *  走 composer 域 catalog 按当前 locale 取词。 */
const BUILTIN_COMMAND_NAMES = ["help", "compact", "clear", "subagent", "plan", "think"] as const;

/** 内置命令的描述 + mention 类型的分组名/说明，均按当前 locale 从 catalog 取。 */
function mentionKindMeta(kind: MentionKind): { group: string; description: string } {
  const cat = composerCatalog().composer;
  switch (kind) {
    case "artifact":
      return { group: cat.menu.groups.artifacts, description: cat.mentions.artifactDesc };
    case "agent":
      return { group: cat.menu.groups.agents, description: cat.mentions.agentDesc };
    case "session":
      return { group: cat.menu.groups.sessions, description: cat.mentions.sessionDesc };
    case "result":
      return { group: cat.menu.groups.results, description: cat.mentions.resultDesc };
  }
}

export const MENTION_KINDS: MentionKind[] = ["artifact", "agent", "session", "result"];

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

/* ── mention P2 匹配增强（mention-menu-limits-design §3 + 2026-11-10 派单）──
 * 打分制：contains(3) > 拼音首字母串 contains(2) > fuzzy 子序列(1)。
 * 拼音仅取首字母（多音字用常用读法，不做整词拼音——成本大收益小）；
 * 子序列匹配允许跳跃但要求顺序（Levenshtein 太重，subsequence 足够）。 */
import { pinyin } from "pinyin-pro";

const pinyinCache = new Map<string, string>();

function pinyinInitials(s: string): string {
  const key = s;
  let v = pinyinCache.get(key);
  if (v === undefined) {
    try {
      // 数组含空格项（原文空白），过滤后拼接——「会话 热更新」→ "hhrgx"。
      v = pinyin(s, { pattern: "first", toneType: "none", type: "array" })
        .filter((x) => x.trim())
        .join("");
    } catch {
      v = "";
    }
    if (pinyinCache.size > 2000) pinyinCache.clear(); // 防长会话膨胀
    pinyinCache.set(key, v);
  }
  return v;
}

function isSubsequence(hay: string, needle: string): boolean {
  let i = 0;
  for (const ch of hay) {
    if (ch === needle[i]) i++;
    if (i === needle.length) return true;
  }
  return false;
}

/**
 * 菜单减负限量（mention-menu-limits-design §2 同构规则）：每组 ≤10 条、
 * 全列表 ≤28 行软上限（超出从末尾低优先组砍）。被截断的组在其首条
 * item.groupTotal 记总数，渲染层组头显示 `shown/total` 徽标。
 * 纯函数、构建侧应用——active 键盘索引与限量后列表天然一致。
 */
export function limitMenuItems(
  items: MenuItem[],
  perGroup = 10,
  totalCap = 28,
): MenuItem[] {
  const groups: Array<{ name: string; all: MenuItem[] }> = [];
  for (const it of items) {
    const g = groups[groups.length - 1];
    if (g && g.name === it.group) g.all.push(it);
    else groups.push({ name: it.group, all: [it] });
  }
  const out: MenuItem[] = [];
  let budget = totalCap;
  for (const g of groups) {
    if (budget <= 0) break;
    const shown = g.all.slice(0, Math.min(perGroup, budget));
    if (g.all.length > shown.length) {
      shown[0] = { ...shown[0], groupTotal: g.all.length };
    }
    out.push(...shown);
    budget -= shown.length;
  }
  return out;
}

export function matchScore(haystack: string, needle: string): number {
  if (!needle) return 0;
  const hay = haystack.toLowerCase();
  const q = needle.toLowerCase();
  if (!q.trim()) return 0;
  if (hay.includes(q)) return 3;
  if (q.match(/[a-z]/) && pinyinInitials(haystack).toLowerCase().includes(q))
    return 2;
  if (isSubsequence(hay, q) && q.length >= 2) return 1;
  return 0;
}

/** LinkedSessionsChip 的 members 投影（@session/@result 候选源，
 *  at-session-and-mention-scope-design §2.4/§5.5）。 */
export interface LinkedMentionEntry {
  id: string;
  title: string;
  /** session type：subagent 排除（P0 边界矩阵）、channel 渠道会话 ⛔。 */
  type?: string | null;
  rel?: string | null;
  /** live_status：waiting（ask_user 挂起）⛔ 禁选——「点击前往」语义。 */
  liveStatus?: string | null;
}

export interface MenuSources {
  skills: SkillSummary[];
  agents: AgentConfig[];
  /** @session（投递）/ @result（引用结论）候选：linked 会话成员。 */
  linkedSessions?: LinkedMentionEntry[];
  /** @result 兜底候选（2026-11-05）：最近会话（引用语义可指向任意历史会话，
   *  linked 未覆盖时补充；@session 投递语义仍 linked-only）。 */
  recentSessions?: Array<{ id: string; title?: string | null; type?: string | null; updated_at?: number }>;
  artifacts: Artifact[];
  /** 当前会话 id：非本会话的 artifact 在菜单里标注来源（跨会话选择）。 */
  sessionId?: string | null;
  /** @ 投递可见范围（agent-team-design §5.2）：团队主会话里 @ 只见本团队
   *  成员实例（blocked 禁选）；缺省（普通/工作台会话）保持全量 agent 菜单。 */
  teamMembers?: import("@/lib/types").TeamMemberRow[];
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
      clear: cat.commands.clearDesc,
      subagent: cat.commands.subagentDesc,
      plan: cat.commands.planDesc,
      think: cat.commands.thinkDesc,
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
    return limitMenuItems(items);
  }

  // mention trigger
  const q = trigger.query.toLowerCase();
  const kindFilter = trigger.mentionKind; // null → all kinds
  const labelQ = trigger.labelQuery;
  const scored: Array<{ item: MenuItem; score: number }> = [];
  const push = (kind: MentionKind, id: string, label: string, detail?: string, disabled?: boolean) => {
    if (kindFilter && kind !== kindFilter) return;
    const searchIn = kindFilter ? label : `${kind}:${label} ${detail ?? ""}`;
    const needle = kindFilter ? labelQ : q;
    // P2 匹配增强：contains > 拼音首字母 > fuzzy 子序列（打分制）。
    const score = matchScore(searchIn, needle);
    if (needle && score === 0) return;
    const meta = mentionKindMeta(kind);
    scored.push({
      score,
      item: {
        kind,
        id,
        label,
        detail: detail || meta.description,
        group: meta.group,
        insert: `@${kind}:${label} `,
        disabled,
      },
    });
  };
  // 注意：items 快照必须在全部 push 循环结束后取（4029f35 曾把它提到
  // 循环前——push 只写 scored，快照恒空 → @ 菜单全灭，2026-11-12 修复）。

  // 团队主会话的 @ 菜单（独立空间语义，2026-10-14 用户定稿）：
  // 顺序 = 成员（投递目标，blocked 禁选）→ 产物（本团队范围，ChatStream 侧
  // 已过滤）；工作流与记忆不出现——投递面保持最小。其他会话维持原顺序
  // （产物 → agent → 工作流 → 记忆，persona 覆盖语义不变）。
  if (src.teamMembers) {
    const cat = composerCatalog().composer;
    const statusOf = (st: string) =>
      ((cat as unknown as Record<string, Record<string, string>>).teamStatus ?? {})[st] ?? st;
    for (const m of src.teamMembers) {
      const blocked = m.status === "blocked";
      push("agent", m.agent_id, m.display_name, `${m.agent_id} · ${statusOf(m.status)}${blocked ? " ⛔" : ""}`, blocked);
    }
    for (const a of src.artifacts) {
      push("artifact", a.id, a.name, a.kind);
    }
    const teamItems: MenuItem[] = scored
      .map((x, i) => ({ ...x, i }))
      .sort((a, b) => b.score - a.score || a.i - b.i)
      .map((x) => x.item);
    return limitMenuItems(teamItems);
  }
  // 跨会话条目 detail 带来源标题——显式 @ 即授权（runtime 解析本就是项目级
  // 按名/id 精确命中），agent 文件工具的边界不受菜单影响。
  for (const a of src.artifacts) {
    const fromOther = a.session_id && a.session_id !== src.sessionId;
    const detail =
      fromOther && a.session_title
        ? `${a.kind} · ${a.session_title}`
        : a.kind;
    push("artifact", a.id, a.name, detail);
  }
  for (const a of src.agents) push("agent", a.id, a.name);
  // @session（投递）/ @result（引用最后结论）——linked members，禁选矩阵见设计稿
  // §2.2/§5.4：waiting ⛔（在等人）、渠道会话 ⛔、subagent 会话 P0 不显示、
  // 自身排除。label=会话标题（服务端按标题在 linked 集合内精确命中）。
  const relLabels: Record<string, string> = {
    spawned: "派生", delegated: "委托", assigned: "派活", watching: "订阅", mounted: "挂载",
  };
  for (const l of src.linkedSessions ?? []) {
    if (l.id === src.sessionId) continue;
    if ((l.type ?? "") === "subagent") continue;
    const isChannel = (l.type ?? "").startsWith("channel");
    const waiting = l.liveStatus === "waiting";
    const blocked = isChannel || waiting;
    const detail = [
      relLabels[l.rel ?? ""] ?? l.rel ?? "",
      waiting ? "⛔ 等你回答" : "",
      isChannel ? "⛔ 渠道会话" : "",
    ].filter(Boolean).join(" · ");
    push("session", l.id, l.title, detail, blocked);
    push("result", l.id, l.title, l.title ? "" : "", false);
  }
  // @result 兜底：最近会话（去重 linked 已推的；自身/subagent/渠道排除）。
  // linked 成员排前面（既有顺序），recent 补在后 —— 无 detail 区分来源。
  const seen = new Set<string>(
    scored.filter((x) => x.item.kind === "result").map((x) => x.item.id),
  );
  for (const r of src.recentSessions ?? []) {
    if (seen.has(r.id) || r.id === src.sessionId) continue;
    if ((r.type ?? "") === "subagent" || (r.type ?? "").startsWith("channel")) continue;
    seen.add(r.id);
    push("result", r.id, r.title || r.id, "");
  }
  const items: MenuItem[] = scored
    .map((x, i) => ({ ...x, i }))
    .sort((a, b) => b.score - a.score || a.i - b.i)
    .map((x) => x.item);
  return limitMenuItems(items);
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

/* ── Mention 显示态（2026-11-10 用户定稿）────────────────────────────
 * 输入框里不再显示裸 `@kind:label` token：选中后插入「emoji + 名」的显示
 * 形态（与气泡 MentionText chip 的图标语义一一对应），发送前经
 * displayMap 还原为协议 token —— runtime 令牌语义零改动。
 * displayMap miss（用户手改了显示文本）时按原样发送，降级不炸。
 */

/** mention kind → 输入框显示用的 emoji 图标（textarea 无法渲染组件）。 */
export const MENTION_DISPLAY_EMOJI: Record<string, string> = {
  agent: "🤖",
  artifact: "📎",
  session: "💬",
  result: "📊",
};

/** 协议 token（`@kind:label `）→ 显示形态（`emoji label `）。 */
export function mentionDisplayToken(kind: string, label: string): string {
  const emoji = MENTION_DISPLAY_EMOJI[kind] ?? "🔗";
  return `${emoji} ${label} `;
}

/**
 * 发送前把输入框里的显示形态还原为协议 token。map 由每次菜单选中时
 * 填充（display 串 → token 串）；同一 display 出现多次全部还原。
 */
/**
 * 显示形态 → 协议 token 的全局映射（模块级单例）：pickItem 每次选中
 * 写入（display 串 → `@kind:label `），发送时 materializeMentions 还原。
 * 故意不清空：draft 跨 slot 保存/恢复后 display 仍在文本里，prune 与
 * materialize 都要能查到；条目为短字符串，累积成本可忽略。
 */
export const mentionDisplayMap = new Map<string, string>();

export function materializeMentions(
  input: string,
  displayMap: Map<string, string>,
): string {
  let out = input;
  // 长串优先，防止 display 互为前缀时短串抢先替换。
  const entries = [...displayMap.entries()].sort(
    (a, b) => b[0].length - a[0].length,
  );
  for (const [display, token] of entries) {
    if (out.includes(display)) out = out.split(display).join(token);
  }
  return out;
}

/**
 * Backspace 原子删除：光标左侧若紧贴一个完整 mention（显示形态或裸
 * token 形态，含尾随空格），返回应删除的起点——一次退格整块删掉；
 * 否则返回 null（走原生逐字符删除）。
 */
export function mentionAtomicDeleteStart(
  input: string,
  caret: number,
): number | null {
  const before = input.slice(0, caret);
  const displays = [...mentionDisplayMap.keys()].sort((a, b) => b.length - a.length);
  for (const d of displays) {
    if (before.endsWith(d)) return caret - d.length;
  }
  // label 可含 ASCII 词间空格（如 "Dev Agent"）；CJK/标点自然终止。
  const m = before.match(/@(artifact|agent|session|result|workflow|memory):([\w.\-\/]+(?: [A-Za-z0-9_.\-\/]+)*) $/);
  if (m) return caret - m[0].length;
  // 用户把尾空格删了又按退格：无尾空格形态也整块删（再删一次的语义）。
  const m2 = before.match(/@(artifact|agent|session|result|workflow|memory):([\w.\-\/]+(?: [A-Za-z0-9_.\-\/]+)*)$/);
  if (m2 && caret - m2[0].length >= 0) return caret - m2[0].length;
  return null;
}

/** Drop resolved mentions whose `@kind:label` token no longer appears.
 * 2026-11-10：输入框显示形态化后，文本里可能是 display（emoji+名）而非
 * token——两种形态任一出现即保留（display 查 mentionDisplayMap）。 */
export function pruneMentions(mentions: ResolvedMention[], text: string): ResolvedMention[] {
  return mentions.filter((m) => {
    const token = `@${m.kind}:${m.label}`;
    if (text.includes(token)) return true;
    // map 值是带尾空格的 insert 串，比对时去尾空格。
    for (const [display, tok] of mentionDisplayMap) {
      if (tok.replace(/\s+$/, "") === token && text.includes(display)) return true;
    }
    return false;
  });
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
