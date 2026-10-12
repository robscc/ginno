"use client";

import type { WorkflowRun } from "@/lib/types";
import { useChatT } from "./i18n";

export type SourceItem = { kind: "wiki" | "web"; ref: string; note?: string };

export type Block =
  | { kind: "text"; text: string }
  // `url` is set for user uploads (data URL). Code-generated images carry a
  // file-ledger `fileId` (+ mtime for cache-busting) resolved via imageUrl().
  | { kind: "image"; url?: string; fileId?: string; name?: string; mtime?: number }
  | { kind: "file"; fileId?: string; name: string; path?: string; fileKind?: string }
  // Slash-skill invocation (/name …): the persisted HumanMessage carries the
  // SKILL.md injection; history replay folds it into this chip + user request.
  | { kind: "skill"; name: string; text?: string }
  | { kind: "widget"; widgetKind: string; data: unknown; renderId?: string }
  | { kind: "ref"; refKind: string; name: string; refId?: string }
  | { kind: "tool"; id?: string; name: string; content: string; pending: boolean; argsPreview?: string }
  // ask_user parked the turn on an ambiguity: the card REPLACES the pending
  // ask_user tool bubble (same tool-call id), and the tool's JSON result folds
  // back into status/answer on tool.end (a stop while parked → "skipped").
  // History replay rebuilds it from the checkpoint (api/messages_ui.py).
  | { kind: "question"; id?: string; question: string; header?: string;
      options: string[]; allowFreeText: boolean;
      status: "pending" | "answered" | "skipped";
      answer?: string; optionIndex?: number | null }
  | { kind: "thinking"; text: string }
  | { kind: "workflow"; run: WorkflowRun }
  // WorldState change announcements (docs/design/world-state-plan.md §7):
  // centered system rows in the transcript ("context chips").
  // 事件契约（i18n-design.md §3）：runtime 可附 i18n_key（dot 路径）+ params，
  // 渲染时翻译（ContextBlocks）；历史重放只有成品 text，原样直显。
  | { kind: "context"; text: string;
      i18n_key?: string; params?: Record<string, string | number> }
  // Answer provenance (docs/citations-design.md): wiki pages / web sources the
  // model cited. Server emits this on history replay; live text blocks are
  // parsed client-side (the trailing <ginno_citations> block is machine meta).
  | { kind: "sources"; items: SourceItem[] }
  // Mid-turn steering (docs/steering-design.md §4.2): a message the user sent
  // while the turn was running, absorbed at a tool-batch boundary. It renders
  // as a full-width band INSIDE the assistant bubble it interrupted — one turn
  // is one bubble, and the band is that injection point made visible. Claude
  // Code shows the same thing as a full-width highlight (changelog 2.1.181).
  // Attachments ride the band (steer-attachments v2). Both the live absorbed
  // band (ChatStream) and the server's replay summary use this shape: images
  // carry a display URL (data URL) when available, files a name (+ optional
  // id/path/kind). Kept tolerant — a summary may omit the image payload.
  | { kind: "steer"; text: string; steerId?: string | null; injectedAt?: number;
      images?: SteerBandImage[]; files?: SteerBandFile[] }
  // ---- subagent（subagent-design.md §6.3，P1 web 侧）----
  // 发起卡片：由 subagent.spawned WS 事件驱动（live 追加，不持久化——持久层
  // 里的 spawn_subagent 工具气泡仍然可见）。状态从 store 的会话元数据实时读。
  | { kind: "subagent_spawn"; sessionId: string; goal: string; constraints?: string;
      acceptance?: string; title?: string; depth?: number;
      origin?: "user" | "agent"; spawnedAt?: number }
  // 结果卡片：由注入的 <ginno_subagent_result> 消息（历史重放折行）与
  // subagent.status 终态事件（live 追加）驱动。
  | { kind: "subagent_result"; sessionId: string; goal?: string; summary: string;
      error?: string }
  // 任务简报卡片：子会话首条消息是 <ginno_subagent_brief> 原文（后接报告
  // 格式与输出纪律两段附录），历史重放折成卡片，避免裸 XML 长文刷屏。
  | { kind: "subagent_brief"; goal: string; constraints?: string;
      acceptance?: string; fork?: boolean; extra?: string; persona?: string;
      notes?: string }
  // 拆分方案卡片（P2 共享契约 1/2）：由 subagent.plan WS 事件驱动（live 追加，
  // 不持久化——未确认的方案没有落 checkpoint 的意义）。组件本体在
  // subagentPlanCard.tsx（本文件已超 1600 行，卡片不再往里塞）。
  | { kind: "subagent_plan"; planId: string; task: string;
      subtasks: import("@/lib/types").SubagentPlanSubtask[];
      status: "pending" | "confirmed" | "cancelled" };

/** 子代状态点（契约用 emoji：🟢 running / ⏳ waiting / ✅ done / ⚠ failed / ⛔ stopped）。
 *  侧栏行与卡片共用同一份映射。 */
export const SUBAGENT_STATUS_META: Record<
  string,
  { glyph: string; label: string; color: string }
> = {
  running: { glyph: "🟢", label: "Running", color: "#22c55e" },
  waiting: { glyph: "⏳", label: "Waiting on subtask", color: "#eab308" },
  done: { glyph: "✅", label: "Completed", color: "#22c55e" },
  failed: { glyph: "⚠️", label: "Failed", color: "#ef4444" },
  stopped: { glyph: "⛔", label: "Stopped", color: "#71717a" },
};

export function subagentStatusMeta(status?: string) {
  return SUBAGENT_STATUS_META[status ?? ""] ?? SUBAGENT_STATUS_META.running;
}

/** 状态三档分桶（2026-10-12 收纳优化）：waiting 并入运行中、stopped 并入
 *  失败（被中止同样无产出）。侧栏子树折叠与主对话委派卡共用同一组桶
 *  ——「运行中 / 失败 / 成功」各为一节，各自独立折叠；行内 glyph 仍保留
 *  五态原貌，桶只是收纳粒度。 */
export type SubagentBucket = "running" | "failed" | "done";

export function subagentStatusBucket(status?: string): SubagentBucket {
  if (status === "failed" || status === "stopped") return "failed";
  if (status === "done") return "done";
  return "running"; // running / waiting / 未知
}

/** 会话行的三档分桶：subagent 走 status；delegation 回放没有 subagent
 *  status，按 stop_reason 折算（与结果卡同一套终态判定）。 */
export function subagentSessionBucket(s: {
  type?: string | null;
  subagent?: { status?: string } | null;
  stop_reason?: string | null;
}): SubagentBucket {
  if (s.type === "delegation") {
    const stop = s.stop_reason ?? "";
    if (!stop) return "running";
    return stop === "error" || stop === "timeout" ? "failed" : "done";
  }
  return subagentStatusBucket(s.subagent?.status);
}

/** 桶的固定渲染序：运行中（活）→ 失败（要人看）→ 成功（归档）。 */
export const SUBAGENT_BUCKET_ORDER: SubagentBucket[] = ["running", "failed", "done"];

/** 状态枚举的展示标签：chat.status.* 命中即译；未识别的状态回退 meta 的
 *  英文标签（SUBAGENT_STATUS_META 的导出形状不变——侧栏等外部使用方仍读它）。 */
export function useStatusLabel() {
  const { tr } = useChatT();
  return (status?: string) => {
    const key = `status.${status ?? ""}`;
    return tr.has(key) ? tr(key) : subagentStatusMeta(status).label;
  };
}

export type SubagentCardBlock =
  | Extract<Block, { kind: "subagent_spawn" }>
  | Extract<Block, { kind: "subagent_result" }>
  | Extract<Block, { kind: "subagent_brief" }>;

/** One attachment chip shown inside a steer band. */
export type SteerBandImage = {
  name?: string;
  url?: string;
  data?: string;
  mediaType?: string;
  media_type?: string;
};
export type SteerBandFile = { id?: string; name: string; path?: string; kind?: string };

export type QuestionBlock = Extract<Block, { kind: "question" }>;
