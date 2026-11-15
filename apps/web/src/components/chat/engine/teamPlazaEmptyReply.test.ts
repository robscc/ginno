// AgentTeam 协作广场「(空回复)」回归测试。
//
// 存在的理由（2026-11-15 用户实录截图）：在团队工作台里发消息，**每次**都紧跟
// 一个「技术经理 · 刚刚 ·（空回复）」的空气泡。
//
// 根因链（三层，缺一不发作）：
//   ① ChatStream.attemptSend 无条件插入 `blocks: []` 的 assistant 占位气泡
//      （普通 turn 靠流事件填充它）；
//   ② 广场投递分支（ws.py 的 AgentTeam 路由）**不起主会话 turn**——只发
//      `team.delivered` + `message.end`，占位气泡永远等不到内容；
//   ③ `team.delivered` handler 只把 liveBySessionRef 置 null，**没把 store 里
//      那个空壳删掉**；随后 `message.end` 的 stampTurnTs 按 turnId 兜底命中它
//      → 盖上 ts → 渲染成「(空回复)」+「刚刚」，且永久留在转录里。
//
// 对照：`turn.stopped` 与 `error` 两条路径早就有
// `.filter((m) => !(m.id === liveMsgId && m.blocks.length === 0))`，
// 注释写的就是同一个「(空回复)」症状——唯独投递分支漏了这道收口。
//
// 两个方向都覆盖：
//   ① 投递后空占位必须消失（防复发）；
//   ② 有内容的气泡必须留下（防修过头把 notice / 已流式正文一起删了）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// message.end 会走 OS 通知路径（shouldNotify → notifyNative → isDesktop →
// navigator.userAgent）。本文件测的是投递收口，不是通知，必须把平台出口 mock
// 掉——否则在 **Node 20（CI）** 上 navigator 不存在 → Unhandled Rejection →
// 整个 vitest run 失败（本地 Node 25 有全局 navigator，所以本地绿 CI 红，
// 2026-11-15 PR #151 首轮实锤）。与 handleNotify.test.ts 同一范式。
const { notifyNative } = vi.hoisted(() => ({
  notifyNative: vi.fn(async (_n: unknown) => true),
}));

vi.mock("@/lib/desktop", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, notifyNative };
});

import { handle } from "@/components/chat/engine/handle";
import type { EngineCtx } from "@/components/chat/engine/ctx";
import type { ChatMsg } from "@/components/chat/streamCore";
import type { Block } from "@/components/chat/blocks";

// 用真实的 ChatMsg / Block，不自造宽松类型——storeRef 的契约就是 ChatMsg[]，
// 自造类型会在 tsc 下与 reducer 的实际读写对不上（role 少了 "system"、
// Block 是判别联合）。测试要贴着生产类型走才有回归价值。
type Msg = ChatMsg;

/** 最小可跑通 team.delivered + message.end 两条路径的 EngineCtx。 */
function makeCtx(): EngineCtx {
  const ref = <T,>(v: T) => ({ current: v });
  const base = {
    i18nRef: ref({ tr: (k: string) => k, evText: (_e: unknown, f: string) => f }),
    activeSidRef: ref<string | null>("s1"),
    storeRef: ref<Record<string, ChatMsg[]>>({}),
    liveBySessionRef: ref<Record<string, string | null>>({}),
    socketsRef: ref<Record<string, unknown>>({}),
    busyBySessionRef: ref<Record<string, boolean>>({}),
    serverRunningRef: ref<Record<string, boolean>>({}),
    compactingBySessionRef: ref<Record<string, boolean>>({}),
    compactStageRef: ref<Record<string, unknown>>({}),
    streamAgentRef: ref<Record<string, string | null>>({}),
    orphanStreamRef: ref<Record<string, boolean>>({}),
    adoptedTurnRef: ref<Record<string, boolean>>({}),
    retryNoticeRef: ref<Record<string, string | null>>({}),
    seenTurnStartRef: ref<Record<string, boolean>>({}),
    permsRef: ref<Record<string, unknown>>({}),
    proposeRef: ref<Record<string, unknown>>({}),
    setPermission: () => {},
    setPropose: () => {},
    steerQ: { takeOldest: () => null },
    attemptSend: () => {},
    session: { agent_id: "techleader" },
    g: {
      sessions: [{ id: "s1", title: "团队广场" }],
      markSessionUnread: () => {},
      clearSessionUnread: () => {},
      watchingSession: () => true,
    },
    syncDisplay: () => {},
  };
  return base as unknown as EngineCtx;
}

const SID = "s1";
const TURN = "turn-plaza-1";

/** 复刻 ChatStream.attemptSend 的乐观插入：用户气泡(sending) + 空 assistant 占位。 */
function seedSend(ctx: EngineCtx, opts: { liveBlocks?: Msg["blocks"] } = {}) {
  const liveBlocks = opts.liveBlocks ?? [];
  ctx.storeRef.current[SID] = [
    { id: "u1", role: "user", blocks: [{ kind: "text", text: "帮忙看看Ginno这个项目" }], turnId: TURN, status: "sending" },
    { id: "live1", role: "assistant", blocks: liveBlocks, turnId: TURN },
  ];
  ctx.liveBySessionRef.current[SID] = "live1";
  ctx.busyBySessionRef.current[SID] = true;
  ctx.serverRunningRef.current[SID] = true;
  ctx.seenTurnStartRef.current[SID] = true;
}

function msgs(ctx: EngineCtx): Msg[] {
  return ctx.storeRef.current[SID] ?? [];
}
function assistants(ctx: EngineCtx): Msg[] {
  return msgs(ctx).filter((m) => m.role === "assistant");
}
/** 取气泡纯文本：Block 是判别联合，image 等变体没有 text 字段。 */
function textOf(m: Msg): string {
  return m.blocks
    .map((b) => ("text" in b && typeof b.text === "string" ? b.text : ""))
    .join("");
}

const DELIVERED = {
  event: "team.delivered",
  receipt: "delivered",
  agent_id: "techleader",
  display_name: "技术经理",
  session_id: "member-sess-1",
  team_id: "team-1",
  instance_id: "inst-1",
  channel: "",
  turn_id: TURN,
};

describe("AgentTeam 广场投递：空占位气泡收口", () => {
  beforeEach(() => {
    notifyNative.mockClear();
    vi.stubGlobal("document", { visibilityState: "visible" });
    vi.stubGlobal("window", { location: { pathname: "/team" } });
    // 忠实模拟 CI 的 Node 20：没有全局 navigator。本地 Node 25 有，不 stub
    // 的话「平台 API 泄漏进 reducer 测试」这类问题本地永远抓不到（PR #151
    // 首轮 CI 红正是如此）。afterEach 的 unstubAllGlobals 负责还原。
    vi.stubGlobal("navigator", undefined);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ── ① 复现用户截图：投递后不许留下空气泡 ────────────────────────────────
  it("投递成功 → 空 assistant 占位被丢弃（不再渲染「(空回复)」）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, DELIVERED);
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    expect(assistants(ctx)).toHaveLength(0);
    // 用户气泡与投递回执必须还在——回执是这条路径唯一的正确反馈。
    const user = msgs(ctx).find((m) => m.role === "user");
    expect(user).toBeTruthy();
    expect(user!.teamDelivery?.receipt).toBe("delivered");
    expect(user!.status).toBeUndefined();
  });

  it("空占位不许被 message.end 盖上时间戳（截图里的「刚刚」）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, DELIVERED);
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    // 症状的本质：空壳拿到 ts → relTime(ts)="刚刚" → 看起来像一条真回复。
    expect(msgs(ctx).some((m) => m.role === "assistant" && m.ts !== undefined)).toBe(false);
  });

  it("连发三条广场消息 → 零空气泡堆积（用户「一定会回复一个空回复」）", () => {
    const ctx = makeCtx();
    for (let i = 0; i < 3; i++) {
      const turn = `turn-${i}`;
      ctx.storeRef.current[SID] = [
        ...(ctx.storeRef.current[SID] ?? []),
        { id: `u${i}`, role: "user", blocks: [{ kind: "text", text: `第${i}条` }], turnId: turn, status: "sending" },
        { id: `live${i}`, role: "assistant", blocks: [], turnId: turn },
      ];
      ctx.liveBySessionRef.current[SID] = `live${i}`;
      handle(ctx, SID, { ...DELIVERED, turn_id: turn });
      handle(ctx, SID, { event: "message.end", turn_id: turn });
    }
    expect(assistants(ctx)).toHaveLength(0);
    expect(msgs(ctx).filter((m) => m.role === "user")).toHaveLength(3);
  });

  it("stalled 回执同样丢空占位，但绝不标 failed（重发=重复投递）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, { ...DELIVERED, receipt: "stalled" });
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    expect(assistants(ctx)).toHaveLength(0);
    const user = msgs(ctx).find((m) => m.role === "user")!;
    expect(user.teamDelivery?.receipt).toBe("stalled");
    expect(user.status).toBeUndefined();
    expect(user.failReason).toBeUndefined();
  });

  it("failed 回执：丢空占位 + 用户气泡标 failed 可重试（原子拒绝一个字没发）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, { ...DELIVERED, receipt: "failed", reason: "agent_blocked", error: "成员被占用" });
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    expect(assistants(ctx)).toHaveLength(0);
    const user = msgs(ctx).find((m) => m.role === "user")!;
    expect(user.status).toBe("failed");
    expect(user.failReason).toBe("成员被占用");
  });

  it("@多人并行投递：N 条回执只留一个用户气泡，且零空气泡", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, { ...DELIVERED, agent_id: "dev", display_name: "开发" });
    handle(ctx, SID, { ...DELIVERED, agent_id: "writer", display_name: "写手", receipt: "stalled" });
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    expect(assistants(ctx)).toHaveLength(0);
    expect(msgs(ctx).filter((m) => m.role === "user")).toHaveLength(1);
    // 保留最严重的一条（stalled > delivered），后到的 delivered 不许覆盖。
    expect(msgs(ctx).find((m) => m.role === "user")!.teamDelivery?.receipt).toBe("stalled");
  });

  // ── ② 正向：有内容的气泡必须留下（防修过头）────────────────────────────
  it("空团队 notice 已写进气泡 → 正文保留（这条路径没有 team.delivered）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    // ws.py 空团队分支：不起 turn，把提示直接推进 live 气泡。
    handle(ctx, SID, { event: "notice", message: "团队还没有成员——先添加成员", turn_id: TURN });
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    const a = assistants(ctx);
    expect(a).toHaveLength(1);
    expect(textOf(a[0])).toContain("先添加成员");
  });

  it("占位已有流式正文 → team.delivered 不许删掉它", () => {
    const ctx = makeCtx();
    seedSend(ctx, { liveBlocks: [{ kind: "text", text: "已经流出来的半截正文" }] });
    handle(ctx, SID, DELIVERED);
    handle(ctx, SID, { event: "message.end", turn_id: TURN });

    const a = assistants(ctx);
    expect(a).toHaveLength(1);
    expect(textOf(a[0])).toBe("已经流出来的半截正文");
  });

  it("成员回帖卡（team.reply）不受影响，正常追加", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, DELIVERED);
    handle(ctx, SID, { event: "message.end", turn_id: TURN });
    handle(ctx, SID, {
      event: "team.reply",
      message_id: "team-reply-inst-1-abcd1234",
      agent_id: "techleader",
      text: "我看完了，项目结构如下……",
      team_id: "team-1",
      instance_id: "inst-1",
      member_session_id: "member-sess-1",
    });

    const a = assistants(ctx);
    expect(a).toHaveLength(1);
    expect(textOf(a[0])).toContain("项目结构");
  });

  it("投递收口后 composer 解锁（busy/serverRunning 归 false）", () => {
    const ctx = makeCtx();
    seedSend(ctx);
    handle(ctx, SID, DELIVERED);
    expect(ctx.busyBySessionRef.current[SID]).toBe(false);
    expect(ctx.serverRunningRef.current[SID]).toBe(false);
    expect(ctx.liveBySessionRef.current[SID]).toBeNull();
  });

  // ── ③ 竞态：上一条的 message.end 晚到，清掉了下一条的 live 引用 ──────────
  // e2e 实测偶发（第一条 @ 投递 + 第二条紧跟）：只按 liveId 过滤时，第二条的
  // 占位漏删 → 又一个「(空回复)」。turnId 兜底判据必须在这种情况下仍然命中。
  it("上一条 message.end 晚到清掉 live 引用 → 下一条占位仍按 turnId 删除", () => {
    const ctx = makeCtx();
    const TURN_A = "turn-A";
    const TURN_B = "turn-B";

    // 第一条：发送 → 投递回执（占位已删、live 已清）
    ctx.storeRef.current[SID] = [
      { id: "uA", role: "user", blocks: [{ kind: "text", text: "第一条" }], turnId: TURN_A, status: "sending" },
      { id: "liveA", role: "assistant", blocks: [], turnId: TURN_A },
    ];
    ctx.liveBySessionRef.current[SID] = "liveA";
    handle(ctx, SID, { ...DELIVERED, turn_id: TURN_A });
    expect(assistants(ctx)).toHaveLength(0);

    // 第二条：发送，live 引用指向新占位
    ctx.storeRef.current[SID] = [
      ...(ctx.storeRef.current[SID] ?? []),
      { id: "uB", role: "user", blocks: [{ kind: "text", text: "第二条" }], turnId: TURN_B, status: "sending" },
      { id: "liveB", role: "assistant", blocks: [], turnId: TURN_B },
    ];
    ctx.liveBySessionRef.current[SID] = "liveB";

    // 第一条的 message.end 晚到——它把 live 引用清成 null（此时已指向 liveB）
    handle(ctx, SID, { event: "message.end", turn_id: TURN_A });
    expect(ctx.liveBySessionRef.current[SID]).toBeNull();

    // 第二条的投递回执：endedLiveId 已是 null，只能靠 turnId 命中
    handle(ctx, SID, { ...DELIVERED, turn_id: TURN_B });
    handle(ctx, SID, { event: "message.end", turn_id: TURN_B });

    expect(assistants(ctx)).toHaveLength(0);
    expect(msgs(ctx).filter((m) => m.role === "user")).toHaveLength(2);
  });

  it("turnId 兜底不许误删别的 turn 的空气泡（只删本轮那一把钥匙）", () => {
    const ctx = makeCtx();
    ctx.storeRef.current[SID] = [
      { id: "uA", role: "user", blocks: [{ kind: "text", text: "A" }], turnId: "turn-A" },
      // 别的 turn 留下的空气泡（例如 refs-only 的历史回复）——不该被本轮顺手删掉
      { id: "other", role: "assistant", blocks: [], turnId: "turn-OTHER" },
      { id: "uB", role: "user", blocks: [{ kind: "text", text: "B" }], turnId: TURN, status: "sending" },
      { id: "liveB", role: "assistant", blocks: [], turnId: TURN },
    ];
    ctx.liveBySessionRef.current[SID] = "liveB";
    handle(ctx, SID, DELIVERED); // turn_id = TURN

    const a = assistants(ctx);
    expect(a).toHaveLength(1);
    expect(a[0].id).toBe("other");
  });
});
