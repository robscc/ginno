// 12-usage — token 用量与上下文占用。等价迁移自 handle() switch（批次 6-2），
// case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { Block } from "@/components/chat/blocks";
import type { ContextState, SessionUsage } from "@/lib/types";
import { upsertTurnMeta } from "../liveBubble";

export function onUsage(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Per-call usage (D2) → TopBar via callback. We forward the TURN
  // increment, not the event's `session` accumulator: that is the
  // runtime's in-memory per-session counter (this session only, wiped on
  // sidecar restart), while the TopBar seeds from GET /usage's
  // persistent whole-tree total — applying the accumulator wholesale made
  // the counter JUMP BACKWARD on every turn (2026-10-31 drift fix; the
  // consumer in AppShell adds the turn onto the fetched base).
  // usage 事件的 turn 增量与圆环更新只作用于**当前活跃会话**（P0，
  // 2026-11-07 混用修复）：TopBar 的 usageDelta/contextState 是全局
  // 单值，而后台会话（定时 task / 并行委托 / 仍在跑的长 turn）一样
  // 产 usage 帧——不过滤会把别的会话的占用覆盖到当前会话的圆环上
  // （「81% 在跳」「多个 session 混用」的根因）。turn-meta 累加仍按
  // 事件所属 sid 落到对应会话的气泡上，不受此守卫影响。
  const active = ctx.activeSidRef.current;
  const t = ev.turn as Partial<SessionUsage> | undefined;
  if (t && typeof t.input_tokens === "number") {
    // turn-meta live（turn 右下角）：本 turn 多次调用逐次累加。**按事件
    // 所属 sid 落块，不受 active 守卫**——后台/子代理会话（非当前活跃
    // 视图）的 turn 统计也要累加，否则切过去看右下角耗时+token 全空
    // （用户反馈 2026-11-17「subagent turn 统计有时不显示」；修复前这段
    // 被误关进 sid===active 守卫，与上方注释意图相悖）。
    const _lid = ctx.liveBySessionRef.current[sid];
    const _cur = (
      _lid &&
      (ctx.storeRef.current[sid] ?? [])
        .find((m) => m.id === _lid)
        ?.blocks.find((b) => b.kind === "turn_meta")
    ) as Extract<Block, { kind: "turn_meta" }> | undefined;
    upsertTurnMeta(ctx, sid, {
      inputTokens: (_cur?.inputTokens ?? 0) + (t.input_tokens ?? 0),
      outputTokens: (_cur?.outputTokens ?? 0) + (t.output_tokens ?? 0),
    });
    // TopBar 全局圆环/增量是全局单值，只给当前活跃会话（防多会话混用，
    // P0 2026-11-07）——这一路才需要 active 守卫。
    if (sid === active) {
      ctx.onUsageChange?.({
        input_tokens: t.input_tokens ?? 0,
        output_tokens: t.output_tokens ?? 0,
        cache_read_tokens: t.cache_read_tokens ?? 0,
        cache_creation_tokens: t.cache_creation_tokens ?? 0,
        calls: 1,
      });
    }
  }
  // 上下文占用（TopBar 圆环）：当前状态，直接替换——仅当前活跃会话
  // 的帧（见上面的 active 守卫）。
  const c = ev.context as ContextState | undefined;
  if (c && typeof c.percent === "number" && sid === active) {
    ctx.onContextChange?.(c);
  }
}
