// 11-context — 上下文公告（updated/microcompacted/compacting/compacted）与
// history.cleared。等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { Block } from "@/components/chat/blocks";
import type { ContextChange } from "@/lib/types";
import { mid } from "../../streamCore";

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
  // Stale tool outputs cleared to placeholders (E2.5) — always visible.
  const n = Number(ev.cleared_tool_outputs ?? 0);
  ctx.storeRef.current[sid] = [
    ...(ctx.storeRef.current[sid] ?? []),
    {
      id: mid(),
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

export function onContextCompacting(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // E3 总结 LLM 调用前发出的瞬态状态:live 气泡状态行
  // 从「正在思考…」切到「压缩中…」。不落 store,不进历史。
  // 2026-11-12 payload 带 {stage,pct}（summarizing=8 / writing=92），
  // 中间段由渲染层时间缓动推进——Claude Code 同款进度条体验。
  const pl = ev as { stage?: string; pct?: number; predicted_s?: number | null };
  if (pl.stage === "done") {
    // 终态（2026-11-12 卡死修复）：compact 是后台任务，结束后若无
    // 后续事件，compacting 镜像永不清 → Composer 永久 readOnly。
    ctx.compactingBySessionRef.current[sid] = false;
    ctx.compactStageRef.current[sid] = null;
    ctx.syncDisplay(sid);
    return;
  }
  ctx.compactingBySessionRef.current[sid] = true;
  ctx.compactStageRef.current[sid] = {
    stage: typeof pl.stage === "string" ? pl.stage : "summarizing",
    predicted_s:
      typeof pl.predicted_s === "number" && pl.predicted_s > 0
        ? pl.predicted_s
        : undefined,
    pct: typeof pl.pct === "number" ? pl.pct : 8,
  };
  ctx.syncDisplay(sid);
}

export function onContextCompacted(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.orphanStreamRef.current[sid] = false;
  ctx.retryNoticeRef.current[sid] = null;
  ctx.syncDisplay(sid);
}
