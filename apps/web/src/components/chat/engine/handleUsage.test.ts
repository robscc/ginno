// engine/handle 的 usage 事件 turn-meta 累加边界回归测试。
//
// 存在的理由（用户反馈 2026-11-17「subagent turn 右下角统计有时不显示」）：
// usage 事件里 turn-meta 的 token 累加曾被误关进 `sid === active` 守卫——
// 而该守卫的本意只是保护 TopBar 全局圆环/增量（全局单值，防多会话混用，
// P0 2026-11-07）。代码上方注释明写「turn-meta 累加仍按事件所属 sid 落到
// 对应会话的气泡上，不受此守卫影响」，实现却相悖：后台/子代理会话（非当前
// 活跃视图）的 usage 帧 sid !== active → token 不累加 → 切过去看 turn 右下角
// 耗时+token 全空。修复把 upsertTurnMeta 移出 active 守卫，TopBar 回调仍门控。
//
// 纯函数测试抓不到这种「守卫范围画错」的结构性错误——必须把真实 usage 帧喂进
// 真实 handle() reducer，断言对应 sid 气泡的 turn_meta 块累加值。

import { describe, expect, it, vi } from "vitest";
import { handle } from "@/components/chat/engine/handle";
import type { EngineCtx } from "@/components/chat/engine/ctx";

// 最小可跑通 EngineCtx（范式同 handleNotify.test.ts）：usage 路径读
// activeSidRef / liveBySessionRef / storeRef / onUsageChange / onContextChange，
// 加上 handle 顶部 compaction 收尾读到的几个 ref。其余字段本路径不触碰，整体 cast。
function makeCtx(overrides: Record<string, unknown> = {}): EngineCtx {
  const ref = <T,>(v: T) => ({ current: v });
  const base = {
    i18nRef: ref({ tr: (k: string) => k, evText: (_e: unknown, f: string) => f }),
    activeSidRef: ref<string | null>(null),
    storeRef: ref<Record<string, unknown[]>>({}),
    liveBySessionRef: ref<Record<string, string | null>>({}),
    socketsRef: ref<Record<string, unknown>>({}),
    busyBySessionRef: ref<Record<string, boolean>>({}),
    compactingBySessionRef: ref<Record<string, boolean>>({}),
    compactStageRef: ref<Record<string, unknown>>({}),
    streamAgentRef: ref<Record<string, string | null>>({}),
    orphanStreamRef: ref<Record<string, boolean>>({}),
    adoptedTurnRef: ref<Record<string, boolean>>({}),
    retryNoticeRef: ref<Record<string, string | null>>({}),
    seenTurnStartRef: ref<Record<string, boolean>>({}),
    session: { agent_id: "dev-agent" },
    g: { sessions: [] as Array<{ id: string; title: string }> },
    syncDisplay: () => {},
    onUsageChange: vi.fn(),
    onContextChange: vi.fn(),
  };
  return { ...base, ...overrides } as unknown as EngineCtx;
}

type MetaBlock = {
  kind: string;
  t0Ms?: number;
  inputTokens?: number;
  outputTokens?: number;
};
type Row = { id: string; blocks: MetaBlock[] };

/** 预置一个带 turn_meta 的 live 气泡（模拟 turn.start 已插入计时块）。 */
function seedLive(ctx: EngineCtx, sid: string, lid: string) {
  (ctx.storeRef.current as Record<string, Row[]>)[sid] = [
    { id: lid, blocks: [{ kind: "turn_meta", t0Ms: 1 }] },
  ];
  (ctx.liveBySessionRef.current as Record<string, string>)[sid] = lid;
}

function turnMeta(ctx: EngineCtx, sid: string): MetaBlock | undefined {
  const lid = (ctx.liveBySessionRef.current as Record<string, string>)[sid];
  const m = ((ctx.storeRef.current as Record<string, Row[]>)[sid] ?? []).find(
    (x) => x.id === lid,
  );
  return m?.blocks.find((b) => b.kind === "turn_meta");
}

describe("handle() usage：turn-meta 按事件 sid 累加（不受 active 守卫）", () => {
  it("回归（2026-11-17）：非活跃会话（子代理）的 usage 也累加 turn-meta token", () => {
    const ctx = makeCtx();
    (ctx.activeSidRef.current as string | null) = "active-session";
    seedLive(ctx, "sub-1", "live-sub");
    handle(ctx, "sub-1", {
      event: "usage",
      turn: { input_tokens: 100, output_tokens: 50 },
    });
    const tm = turnMeta(ctx, "sub-1");
    expect(tm?.inputTokens).toBe(100);
    expect(tm?.outputTokens).toBe(50);
  });

  it("同一 turn 多次 usage 逐次累加（后台会话）", () => {
    const ctx = makeCtx();
    (ctx.activeSidRef.current as string | null) = "other";
    seedLive(ctx, "sub-1", "live-sub");
    handle(ctx, "sub-1", { event: "usage", turn: { input_tokens: 100, output_tokens: 50 } });
    handle(ctx, "sub-1", { event: "usage", turn: { input_tokens: 30, output_tokens: 20 } });
    const tm = turnMeta(ctx, "sub-1");
    expect(tm?.inputTokens).toBe(130);
    expect(tm?.outputTokens).toBe(70);
  });

  it("TopBar 全局增量仍只给活跃会话（防混用守卫保留，非活跃 sid 不回调）", () => {
    const ctx = makeCtx();
    (ctx.activeSidRef.current as string | null) = "active-session";
    seedLive(ctx, "sub-1", "live-sub");
    handle(ctx, "sub-1", { event: "usage", turn: { input_tokens: 100, output_tokens: 50 } });
    expect(ctx.onUsageChange).not.toHaveBeenCalled();
    // 活跃会话自己的 usage 才回调 TopBar。
    seedLive(ctx, "active-session", "live-active");
    handle(ctx, "active-session", { event: "usage", turn: { input_tokens: 10, output_tokens: 5 } });
    expect(ctx.onUsageChange).toHaveBeenCalledTimes(1);
  });

  it("活跃会话的 turn-meta 同样累加（修复不回归本会话路径）", () => {
    const ctx = makeCtx();
    (ctx.activeSidRef.current as string | null) = "s1";
    seedLive(ctx, "s1", "live-1");
    handle(ctx, "s1", { event: "usage", turn: { input_tokens: 7, output_tokens: 3 } });
    const tm = turnMeta(ctx, "s1");
    expect(tm?.inputTokens).toBe(7);
    expect(tm?.outputTokens).toBe(3);
    expect(ctx.onUsageChange).toHaveBeenCalledTimes(1);
  });
});