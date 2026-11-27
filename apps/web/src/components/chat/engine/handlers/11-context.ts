// 11-context — 上下文公告（updated/microcompacted/compacting/compacted）与
// history.cleared。等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { Block } from "@/components/chat/blocks";
import type { ContextChange } from "@/lib/types";
import { mid } from "../../streamCore";
import { flushSteerQueue } from "../steer";

export function onContextUpdated(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // WorldState change announcement (world-state-plan §7). Chip display
  // level table: environment-only changes (date rollover) stay SILENT in
  // the UI; everything else gets a centered context row.
  const changes = ((ev.changes as ContextChange[]) || []).filter(Boolean);
  const visible = changes.filter((c) => c.section !== "environment");
  if (!visible.length) return; // environment-only (date rollover) = silent
  // 事件契约（i18n-design.md §3）：单条 change 可带 i18n_key+params，
  // 透传给 ContextBlocks 渲染时翻译；旧 runtime 只有成品 summary，直显。
  const rows = visible.map((c): Block => {
    const extra = c as { i18n_key?: unknown; params?: unknown };
    return {
      kind: "context",
      text: c.summary,
      ...(typeof extra.i18n_key === "string"
        ? {
            i18n_key: extra.i18n_key,
            ...(extra.params != null ? { params: extra.params as Record<string, string | number> } : {}),
          }
        : {}),
    };
  });
  ctx.storeRef.current[sid] = [
    ...(ctx.storeRef.current[sid] ?? []),
    { id: mid(), role: "system" as const, blocks: rows },
  ];
  ctx.syncDisplay(sid);
}

export function onContextMicrocompacted(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  const n = Number(ev.cleared_tool_outputs ?? 0);
  const rows = ctx.storeRef.current[sid] ?? [];
  const noticeId = typeof ev.notice_id === "string" ? ev.notice_id : undefined;
  // History can already contain this notice when its live event arrives.
  if (noticeId && rows.some((row) => row.id === noticeId)) return;
  ctx.storeRef.current[sid] = [
    ...rows,
    {
      id: noticeId || mid(),
      role: "system" as const,
      blocks: [
        {
          kind: "context",
          // 契约：key 命中渲染时翻译（chat.context.microcompacted）；
          // text 是旧 runtime / key 未命中时的英文兜底（与历史版本一致）。
          text: `Cleared ${n} older tool outputs to save context; call the tools again to re-fetch when needed.`,
          i18n_key: "chat.context.microcompacted",
          params: { n },
        },
      ],
    },
  ];
  ctx.syncDisplay(sid);
}

export type CompactionStage = "summarizing" | "writing";

export function compactionStageAfterFrame(stage: CompactionStage | null, ev: ServerEvent): CompactionStage | null {
  if (ev.event === "context.compacting") {
    if (ev.stage === "done") return null;
    return ev.stage === "writing" ? "writing" : "summarizing";
  }
  if (ev.event === "turn.state") {
    if (ev.compacting === false) return null;
    if (ev.compacting === true) {
      if (ev.compaction_stage === "writing" || ev.compaction_stage === "summarizing") return ev.compaction_stage;
      return stage ?? "summarizing";
    }
  }
  return ev.event === "context.compacted" ? null : stage;
}

export function onContextCompacting(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  ctx.compactStageRef.current[sid] = compactionStageAfterFrame(ctx.compactStageRef.current[sid] ?? null, ev);
  if (ev.event === "context.compacting" && ev.stage === "done" &&
      !ctx.serverRunningRef.current[sid] && !ctx.busyBySessionRef.current[sid] &&
      !ctx.liveBySessionRef.current[sid] && ctx.steerQ.itemsFor(sid)?.length) {
    flushSteerQueue(ctx, sid);
  }
}

export function onContextCompacted(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  onContextCompacting(ctx, sid, ev);
  // History compaction announcement (E3) — always visible.
  const n = Number(ev.compacted_messages ?? 0);
  ctx.storeRef.current[sid] = [
    ...(ctx.storeRef.current[sid] ?? []),
    {
      id: mid(),
      role: "system" as const,
      blocks: [
        {
          kind: "context",
          text: `Conversation compacted: ${n} older messages were replaced with a summary; recent messages are kept verbatim.`,
          i18n_key: "chat.context.compacted",
          params: { n },
        },
      ],
    },
  ];
  ctx.syncDisplay(sid);
}

export function onHistoryCleared(ctx: EngineCtx, sid: string) {
  // /clear 已把 checkpoint 的 messages 清空：本地 transcript 整体丢弃，
  // 流式状态一并复位（ensureLive 会在空 store 上为紧随其后的 /clear
  // 收据 notice 新开一个气泡，作为新对话的第一条）。
  ctx.storeRef.current[sid] = [];
  ctx.compactStageRef.current[sid] = null;
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.orphanStreamRef.current[sid] = false;
  ctx.retryNoticeRef.current[sid] = null;
  ctx.syncDisplay(sid);
}
