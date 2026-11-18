// streamCore — ChatStream 的纯函数层（自 ChatStream.tsx 机械拆出，行为零变化）。
// 消息气泡/发送载荷类型、turn id 生成、block reducer（applyBlock）与
// 历史/重试载荷重建。无 React 依赖，可单测。

import type { ComposerImage, ComposerFile } from "@/lib/composerAttachments";
import type { ResolvedMention } from "@/components/chat/commandMenu";
import type { Block, QuestionBlock } from "@/components/chat/blocks";
import type { TeamReplyMeta } from "@/lib/types";

export interface ChatMsg {
id: string;
// "system" = WorldState context chip rows (centered, not a bubble)
role: "user" | "assistant" | "system";
blocks: Block[];
agentId?: string | null;
agentName?: string;
// AgentTeam 回帖（agent-team-design.md §5.2 要点 2）：非空表示这条 assistant
// 气泡是某成员从自己的 session 回帖到主会话的产出——渲染成回帖卡（可折叠、
// 点「展开全文」跳成员工作台），而不是普通气泡。仅 team 主会话会出现。
teamReply?: TeamReplyMeta | null;
// AgentTeam 投递回执（backlog P1-1）：挂在**用户那条消息**上，渲染成气泡下方
// 的一行 sysline。三态语义严格区分（设计 §5.3.3）：
//   delivered = 已送达；stalled = **也已发送**只是 turn 未起跑（禁一键重发，
//   重发就是重复投递）；failed = 确定没发（blocked 原子拒绝 / 成员不存在）。
teamDelivery?: {
  receipt: "delivered" | "stalled" | "failed";
  channel?: string;        // steer | turn | queue
  reason?: string;         // agent_blocked / unknown_member / …
  error?: string;
  displayName?: string;
  memberSessionId?: string;
  teamId?: string;
  instanceId?: string;
} | null;
turnId?: string; // per-turn trace UUID (shown on the bubble, greppable in sidecar logs)
// 气泡时间戳（unix 秒，客户端时钟）。turn 收口（message.end）时打——完成态
// 的相对时间标签（"N分钟前"）以它为源；历史重放没有时间戳源则不渲染标签
// （2026-10-29 用户反馈：1h 后气泡仍显示「刚刚」，旧实现是硬编码字面量）。
ts?: number;
// Delivery state — user bubbles only. "sending" = in flight to the sidecar;
// "failed" = never delivered (red ❗, click to retry). Successful delivery
// clears it back to undefined.
status?: "sending" | "failed";
failReason?: string;
// Immutable snapshot of everything the turn carries, kept on the bubble so
// a failed send can be retried or re-edited without losing content.
sendPayload?: SendPayload;
// Assistant turn-error card: the input WAS delivered but the run errored
// (model/provider failure etc.). blocks[0] holds the error text;
// sendPayload carries the originating user turn for the retry button.
error?: boolean;
// Assistant bubble that was streaming when the turn errored — rendered with
// a red border + "回复中断" label so it doesn't look like a normal reply.
failed?: boolean;
// Error cards only: id of the originating user bubble. Retry operates on
// THAT bubble in place — no duplicate message is appended.
sourceMsgId?: string;
}
// The mid-turn steering queue state machine (SteerItem, the per-session queue,
// send / recall / ack handling) now lives in @/lib/steerQueue, shared with the
// floating quick-chat window (steer-queue-shared-brief §3.1).

export interface SendPayload {
text: string;
images: Attachment[];
files: FileAttachment[];
mentions: ResolvedMention[];
agentId: string | null;
}
export const newTurnId = () =>
typeof crypto !== "undefined" && "randomUUID" in crypto
  ? crypto.randomUUID()
  : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
// C+ 方案①：composer chip 的轻量关键词推荐（原型 REC_RULES）。纯客户端
// 启发式，顺序敏感——命中第一条规则即停；只展示「推荐」小标签，绝不自动选中。
const AGENT_REC_RULES: Array<{ agentId: string; kws: string[] }> = [
{ agentId: "research", kws: ["调研", "研究", "查一下", "搜", "资料", "对比", "了解", "竞品"] },
{ agentId: "writer", kws: ["写一篇", "文档", "文章", "总结", "润色", "周报", "邮件", "大纲"] },
{ agentId: "workflow-dev", kws: ["工作流", "workflow", "流水线"] },
{ agentId: "dev", kws: ["代码", "bug", "实现", "修复", "重构", "报错", "函数", "部署"] },
];

export function recommendAgentId(text: string, existing: ReadonlySet<string>): string | null {
const t = text.toLowerCase();
for (const rule of AGENT_REC_RULES) {
  if (!existing.has(rule.agentId)) continue; // 自定义/已删除的 agent 不参与推荐
  if (rule.kws.some((k) => t.includes(k))) return rule.agentId;
}
return null;
}

/** 失败 turn 的恢复横幅（ErrorCard）是否仍该渲染（2026-11-18 用户反馈）。
 *
 * 横幅是「会话停在失败终态」的恢复入口——只在该 error 消息仍是**会话最后
 * 一个 turn**（其后没有更新的用户/助手消息）时渲染。新一轮对话一旦开始，
 * 旧失败横幅就是过时入口（点击会重放旧 turn，把旧输入插回历史），整体
 * 隐藏；该 turn 已生成的 thinking/部分回复气泡（m.failed）不受影响，
 * 历史/checkpoint 数据绝不动——纯渲染层判定。
 *
 * system 行（context chips / 拆分方案卡）不算 turn 内容：失败后注入的系统
 * 块不该顶掉横幅。user 消息即使投递失败（status=failed）也算——用户已
 * 表达新意图，恢复重心应转到那条消息自己的重发入口。边界②：新 turn 又
 * 失败时新横幅出现、旧的消失，转录里始终至多一张。
 */
export function failedTurnBannerVisible(
  messages: ReadonlyArray<Pick<ChatMsg, "role">>,
  index: number,
): boolean {
  for (let i = index + 1; i < messages.length; i++) {
    const role = messages[i].role;
    if (role === "user" || role === "assistant") return false;
  }
  return true;
}

export interface PermissionPrompt {
tool: string;
args: unknown;
}

export interface VersionPropose {
workflow_id: string;
from_version: number;
diff: string;
rationale: string;
}

let _mid = 0;
export const mid = () => `m${++_mid}`;
// S6: localStorage key for the unsaved summarize draft (24h TTL, see openSummarize).
export const SUMMARIZE_DRAFT_KEY = "ginno-summarize-draft";
const SUMMARIZE_DRAFT_TTL = 24 * 3600 * 1000;

export interface SummarizeDraft {
dsl: Record<string, unknown>;
sourceSessionId?: string;
sourceLabel?: string;
savedAt: number;
}

export function readSummarizeDraft(): SummarizeDraft | null {
try {
  const raw = localStorage.getItem(SUMMARIZE_DRAFT_KEY);
  if (!raw) return null;
  const d = JSON.parse(raw) as SummarizeDraft;
  if (d?.dsl && typeof d.savedAt === "number" && Date.now() - d.savedAt < SUMMARIZE_DRAFT_TTL) {
    return d;
  }
} catch {
  /* corrupted draft */
}
return null;
}

/** One-line summary of a tool call's args for the live-run tool row
 *  (workflow-ux-redesign P1): first string-ish value, truncated to 50 chars. */
export function toolArgsPreview(args: unknown): string {
if (!args || typeof args !== "object") return "";
for (const v of Object.values(args as Record<string, unknown>)) {
  if (typeof v === "string" && v.trim()) {
    const t = v.trim();
    return t.length > 50 ? t.slice(0, 49) + "…" : t;
  }
}
return "";
}
// Composer attachment shapes now live in @/lib/composerAttachments (shared with
// the floating quick-chat window). Aliased so the rest of this file is unchanged.
export type Attachment = ComposerImage;
export type FileAttachment = ComposerFile;

export const TABLE_KINDS = new Set(["spreadsheet", "table"]);

// readImage now lives in @/lib/composerAttachments (shared with the floating
// window). Its 400KB / 1600px / JPEG 0.85 thresholds are unchanged.

// Patterns that indicate a tool returned "no results" — hide these blocks to reduce noise.
// Covers both the builtin tool phrasing ("(no matches)") and the MCP filesystem
// server's phrasing ("No matches found"), so empty search results don't pile up
// as collapsed panels the agent already explains in prose.
const EMPTY_TOOL_RESULT_RE =
/^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

export function isEmptyToolResult(content: string): boolean {
return EMPTY_TOOL_RESULT_RE.test(content);
}
/** ask_user tool.end content → the question card's final state. The receipt
 * is JSON ({ok, skipped, source, option_index, answer, free_text}); a stop
 * while parked persists the literal "(interrupted)" instead. Anything
 * unparseable also reads as skipped — a card left "pending" would keep the
 * session stuck in the running state (hasPendingTool counts it). */
export function foldQuestionResult(q: QuestionBlock, content: string): QuestionBlock {
if (content.trim() !== "(interrupted)") {
  try {
    const r = JSON.parse(content);
    if (r && typeof r === "object") {
      if (r.skipped || r.source === "skipped") return { ...q, status: "skipped" };
      // 多问题回执（契约 v2）：results 逐题回填子题（skipped 挂子题上）。
      if (r.multi && Array.isArray(r.results)) {
        return {
          ...q,
          status: "answered",
          questions: (q.questions ?? []).map((sq, i) => {
            const rr = r.results[i];
            if (!rr || typeof rr !== "object" || rr.source === "skipped" || !rr.answer) {
              return { ...sq, skipped: true };
            }
            return {
              ...sq,
              answer: String(rr.answer),
              optionIndex: typeof rr.option_index === "number" ? rr.option_index : null,
            };
          }),
        };
      }
      return {
        ...q,
        status: "answered",
        answer: String(r.answer ?? r.free_text ?? ""),
        optionIndex: typeof r.option_index === "number" ? r.option_index : null,
      };
    }
  } catch {
    /* not JSON — falls through to skipped */
  }
}
return { ...q, status: "skipped" };
}

/** Close a dead turn's unfinished blocks so `running` unsticks: pending tool
 * bubbles become "(interrupted)"; pending question cards flip to skipped via
 * their own status field — mirroring what the backend's stop-heal persists
 * ("(interrupted)" tool result), which history replay reads as skipped.
 * llm_call 行同治：pending 的「请求中…」定格为中性 interrupted 态（哨兵
 * error 值，渲染成灰字而非失败红/琥珀）——否则 turn 已停、秒表永远转。 */
export function closePendingBlocks(blocks: Block[]): Block[] {
return blocks.map((b) =>
  b.kind === "tool" && b.pending
    ? { ...b, pending: false, content: b.content === "…" ? "(interrupted)" : b.content }
    : b.kind === "question" && b.status === "pending"
      ? { ...b, status: "skipped" as const }
      : b.kind === "llm_call" && b.pending
        ? { ...b, pending: false, ok: false, error: "interrupted" }
        : b,
);
}

/** Rebuild a retry payload from a history user bubble's blocks — used to
 * re-surface a persisted turn-error card (with working retry) after a reload
 * or route/session switch. Image data URLs round-trip through the checkpoint.
 * Assistant-side kinds (tool/question/widget/…) are ignored BY DESIGN — only
 * user content round-trips into a retry; don't "complete" the switch. */
export function payloadFromBlocks(blocks: Block[], agentId: string | null): SendPayload {
const payload: SendPayload = { text: "", images: [], files: [], mentions: [], agentId };
for (const b of blocks) {
  if (b.kind === "text") {
    payload.text = payload.text ? `${payload.text}\n${b.text}` : b.text;
  } else if (b.kind === "skill") {
    // History-replayed slash-skill turn: resend as the original invocation
    // so the server re-runs the skill substitution.
    const line = b.text ? `/${b.name} ${b.text}` : `/${b.name}`;
    payload.text = payload.text ? `${payload.text}\n${line}` : line;
  } else if (b.kind === "file") {
    payload.files.push({
      id: b.fileId ?? "",
      name: b.name,
      path: b.path ?? "",
      kind: b.fileKind ?? "",
    });
  } else if (b.kind === "image") {
    // Only user-upload data URLs round-trip into a retry payload; generated
    // images (fileId-based) are display-only and skipped here.
    if (!b.url) continue;
    const m = /^data:([^;]+);base64,(.*)$/.exec(b.url);
    if (m) payload.images.push({ data: m[2], mediaType: m[1], preview: b.url, name: "image" });
  }
}
return payload;
}
/** 追加文本/思考增量，与服务端 AIMessageChunk 的「按 content block 索引合并」
 *  对齐：模型会在正文之间零星吐出 thinking 片段（反之亦然），若只在末尾同类才
 *  合并，实时视图会把它们渲染成夹在正文中的独立小卡片，而重放视图（历史）是
 *  合并后的样子——两者不一致（用户反馈 2026-10-01：thinking 与 content 顺序
 *  看起来不对）。合并范围限定在「最近一个非文本/思考块之后」，工具气泡、
 *  steering 带、卡片等仍然是边界。 */
function appendStreamDelta(
  blocks: Block[],
  kind: "text" | "thinking",
  t: string,
): Block[] {
  if (!t) return blocks;
  let start = 0;
  for (let i = blocks.length - 1; i >= 0; i--) {
    const k = blocks[i].kind;
    if (k !== "text" && k !== "thinking") {
      start = i + 1;
      break;
    }
  }
  for (let i = blocks.length - 1; i >= start; i--) {
    if (blocks[i].kind === kind) {
      const next = blocks.slice();
      const cur = next[i] as { kind: "text" | "thinking"; text: string };
      next[i] = { kind, text: cur.text + t };
      return next;
    }
  }
  return [...blocks, { kind, text: t }];
}

export function applyBlock(blocks: Block[], ev: { event: string; [k: string]: unknown }): Block[] {
switch (ev.event) {
  case "token.delta":
    return appendStreamDelta(blocks, "text", (ev.content as string) || "");
  case "thinking.delta":
    return appendStreamDelta(blocks, "thinking", (ev.content as string) || "");
  case "tool.start":
    return [...blocks, { kind: "tool", id: ev.id as string | undefined, name: ev.name as string, content: "…", pending: true }];
  case "tool.args": {
    // Attach the tool call's args preview (e.g. the bash command) to the
    // pending bubble so the user sees WHAT is running, not just the label.
    const id = ev.id as string | undefined;
    const preview = ev.preview as string;
    if (!id || !preview) return blocks;
    let matched = false;
    return blocks.map((b) => {
      if (b.kind !== "tool") return b;
      if (!matched && b.id === id) {
        matched = true;
        return { ...b, argsPreview: preview };
      }
      return b;
    });
  }
  case "tool.end": {
    const id = ev.id as string | undefined;
    const name = ev.name as string | undefined;
    const content = ev.content as string;
    // ask_user: the question card replaced the tool bubble, so the result
    // folds into the card instead. Checked FIRST — the empty-result filter
    // below must never get a chance at a question block (it only drops
    // tool blocks, but the fold has to win before that anyway).
    const qi = id ? blocks.findIndex((b) => b.kind === "question" && b.id === id) : -1;
    if (qi >= 0) {
      const next = blocks.slice();
      next[qi] = foldQuestionResult(next[qi] as QuestionBlock, content);
      return next;
    }
    // 定位这次 tool.end 该落到哪个工具块上。
    //
    // 2026-11-15 修复「结果错位 + 执行中气泡卡死」。旧实现是
    //   blocks.map(…) + !found && (id ? b.id===id : name ? b.name===name : b.pending)
    // 两个致命点：name 兜底**不要求 pending**（已完成的同名块也参与匹配），
    // 且 map 从头扫、`!found` 取第一个 → 命中的是**最早**那个同名块，而不是
    // 真正在跑的那个。服务端 BLOCK_PREFIX 分支曾漏发 id（engine.py 已同步修），
    // 于是被拦工具的 denial 文案覆盖到前面已完成的气泡上，真正被拦的那个永远
    // 停在「执行命令中…」。同名工具一个 turn 内多次调用（bash 重试最常见）即触发。
    //
    // 现在：有 id 走精确匹配（**不限 pending**，保证重连重放幂等）；无 id 才退到
    // 名字，且**只认 pending、从后往前取最近一个**——即使服务端再漏 id，也只会
    // 落到正在跑的那个块上，不会污染历史气泡。
    let ti = -1;
    if (id) {
      ti = blocks.findIndex((b) => b.kind === "tool" && b.id === id);
    } else {
      for (let i = blocks.length - 1; i >= 0; i--) {
        const b = blocks[i];
        if (b.kind !== "tool" || !b.pending) continue;
        if (!name || b.name === name) {
          ti = i;
          break;
        }
      }
    }
    // Hide tool blocks that returned "no results" to reduce noise.
    // 只删**这一个**目标块：旧实现在 name-only 时走 filter 全删，会把一个 turn 里
    // 所有同名工具块一次删光（实测 2 → 0）。
    if (isEmptyToolResult(content)) {
      if (ti < 0) return blocks;
      return blocks.filter((_, i) => i !== ti);
    }
    if (ti >= 0) {
      const next = blocks.slice();
      next[ti] = {
        ...(next[ti] as Extract<Block, { kind: "tool" }>),
        content,
        pending: false,
      };
      return next;
    }
    // 没有任何可配对的块（tool.start 丢帧/未被 adopt，或拦截发生在建块之前）：
    // 追加一个已完成的工具块，而不是静默丢弃结果——与下面 user.question 分支的
    // `[...blocks, card]` 兜底对齐。旧实现 map 原样返回，结果直接消失。
    // 特殊工具（render/workflow/artifact）不受影响：服务端 tool.start 与 tool.end
    // 对它们的跳过是对称的，根本不会发到这里。
    return [
      ...blocks,
      { kind: "tool", id, name: name ?? "tool", content, pending: false },
    ];
  }
  case "user.question": {
    // ask_user parked the turn: the card REPLACES the pending ask_user tool
    // bubble with the same call id (the user sees "询问中…" first, then the
    // card). Merging by id is what makes a reconnect re-emit a no-op instead
    // of a duplicate card — an existing card may already carry an optimistic
    // answer, so it is kept as-is.
    const id = (ev.id as string | undefined) || undefined;
    if (id && blocks.some((b) => b.kind === "question" && b.id === id)) return blocks;
    // 多问题（契约 v2）：questions 数组逐题进卡；单问题走旧字段。
    const rawQs = ev.questions as
      | Array<{ question?: string; header?: string; options?: string[]; allow_free_text?: boolean }>
      | undefined;
    const subs =
      Array.isArray(rawQs) && rawQs.length >= 2
        ? rawQs
            .filter((q) => (q?.question ?? "").trim())
            .map((q) => ({
              question: q.question ?? "",
              header: (q.header || "").trim() || undefined,
              options: Array.isArray(q.options) ? q.options : [],
              allowFreeText: q.allow_free_text !== false,
            }))
        : undefined;
    const card: QuestionBlock = {
      kind: "question",
      id,
      question: (ev.question as string) || "",
      header: (ev.header as string) || undefined,
      options: subs ? [] : (ev.options as string[]) || [],
      allowFreeText: subs ? true : ev.allow_free_text !== false,
      status: "pending",
      ...(subs ? { multi: true, questions: subs } : {}),
    };
    const ti = id ? blocks.findIndex((b) => b.kind === "tool" && b.id === id) : -1;
    if (ti >= 0) {
      const next = blocks.slice();
      next[ti] = card;
      return next;
    }
    return [...blocks, card];
  }
  case "widget.emit":
    return [
      ...blocks,
      {
        kind: "widget",
        widgetKind: ev.kind as string,
        data: ev.data,
        renderId: (ev.render_id as string | undefined) || undefined,
      },
    ];
  case "workflow.emit":
    return [...blocks, { kind: "workflow", run: ev.run as import("@/lib/types").WorkflowRun }];
  case "ref.emit":
    return [
      ...blocks,
      {
        kind: "ref",
        refKind: ev.kind as string,
        name: ev.name as string,
        refId: ev.ref_id as string | undefined,
        // 远端会话转发帧带的来源标记（remote_turn.py enrich）——本机不存在
        // 该路径，RefChip 据此降级为远端提示。
        remote: (ev.remote as boolean) || undefined,
        peer: (ev.peer as string) || undefined,
      },
    ];
  case "image.emit":
    // Code-generated image (bash) surfaced inline; URL resolved from fileId.
    return [
      ...blocks,
      {
        kind: "image",
        fileId: ev.file_id as string,
        name: ev.name as string | undefined,
        mtime: ev.mtime as number | undefined,
      },
    ];
  // ---- LLM 请求级可观测（T3）：llm.request / llm.response 按 (req_id,
  // attempt) 配对合并成一条 llm_call 行（契约见 graph._LLM_EVENT_FIELDS /
  // engine custom 分支白名单）。req_id 跨重试稳定、attempt 逐次递增——
  // 合并键必须含 attempt，否则重试的失败尝试会被成功尝试覆盖掉。
  case "llm.request": {
    const key = llmFrameKey(ev);
    if (!key) return blocks; // 无配对键的退化帧：不建无法合并的行
    // 重连重放幂等：同 (reqId, attempt) 已存在 → 原样返回（与 tool.end /
    // user.question 的按 id 合并同一哲学）。
    if (blocks.some((b) => b.kind === "llm_call" && b.reqId === key.reqId && b.attempt === key.attempt)) {
      return blocks;
    }
    return [...blocks, { kind: "llm_call", ...key, ...llmFrameMeta(ev), pending: true }];
  }
  case "llm.response": {
    const key = llmFrameKey(ev);
    if (!key) return blocks;
    const patch = llmResponsePatch(ev);
    const i = blocks.findIndex(
      (b) => b.kind === "llm_call" && b.reqId === key.reqId && b.attempt === key.attempt,
    );
    if (i >= 0) {
      // 原地更新：位置不变、数组长度不变——「请求中…」行补上耗时与 token。
      const next = blocks.slice();
      next[i] = { ...(next[i] as Extract<Block, { kind: "llm_call" }>), ...llmFrameMeta(ev), ...patch };
      return next;
    }
    // request 丢帧/晚认领（孤儿流）：兜底追加一条**已完成**行，不静默丢结果
    // ——与 tool.end 无配对块时的 append 兜底对齐。pending 显式置 false
    // （patch 是 Partial，展开后必填字段需显式给出）。
    return [...blocks, { kind: "llm_call", ...key, ...llmFrameMeta(ev), ...patch, pending: false }];
  }
  default:
    return blocks;
}
}

/** llm.request/response 帧的配对键：req_id 必须是非空字符串；attempt 缺省
 *  归 1（后端契约保证 ≥1，这里只防畸形帧）。返回 null = 无法配对。 */
function llmFrameKey(ev: { [k: string]: unknown }): { reqId: string; attempt: number } | null {
  const reqId = typeof ev.req_id === "string" ? ev.req_id : "";
  if (!reqId) return null;
  const attempt =
    typeof ev.attempt === "number" && Number.isFinite(ev.attempt) && ev.attempt >= 1
      ? Math.floor(ev.attempt)
      : 1;
  return { reqId, attempt };
}

/** 帧上的模型元数据（request/response 都带同一份）：只收非空值——response
 *  元数据提取失败退化为 {} 时，展开空对象不会清掉 request 已填的字段。 */
function llmFrameMeta(ev: { [k: string]: unknown }): Partial<Extract<Block, { kind: "llm_call" }>> {
  return {
    ...(typeof ev.model === "string" && ev.model ? { model: ev.model } : {}),
    ...(typeof ev.provider === "string" && ev.provider ? { provider: ev.provider } : {}),
    ...(typeof ev.messages_count === "number" ? { messagesCount: ev.messages_count } : {}),
  };
}

/** response 帧 → llm_call 行的终态补丁。usage 缺失（provider 未上报）时
 *  省略 token 字段，不造数。ok 缺省按成功（后端契约必带 ok，防御畸形帧）。 */
function llmResponsePatch(ev: { [k: string]: unknown }): Partial<Extract<Block, { kind: "llm_call" }>> {
  const patch: Partial<Extract<Block, { kind: "llm_call" }>> = {
    pending: false,
    ok: ev.ok !== false,
  };
  if (typeof ev.error === "string" && ev.error) patch.error = ev.error;
  if (typeof ev.elapsed_ms === "number" && Number.isFinite(ev.elapsed_ms)) patch.elapsedMs = ev.elapsed_ms;
  const u = ev.usage;
  if (u && typeof u === "object") {
    const r = u as Record<string, unknown>;
    if (typeof r.input_tokens === "number") patch.inputTokens = r.input_tokens;
    if (typeof r.output_tokens === "number") patch.outputTokens = r.output_tokens;
    if (typeof r.total_tokens === "number") patch.totalTokens = r.total_tokens;
    if (typeof r.cache_read_tokens === "number") patch.cacheReadTokens = r.cache_read_tokens;
  }
  return patch;
}

