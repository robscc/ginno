// engine/handle 的 OS 通知边界回归测试。
//
// 存在的理由（docs/notifications-audit-2026-11-14.md P0-1）：ask_user 的 OS 通知
// 曾被塞进 `case "tool.start" / "tool.args" / "tool.end" / "user.question"` 的
// **共享 case 体**，而 `token.delta` / `thinking.delta` 又 fall through 进同一块。
// delta 帧不带 ev.id，去重键退化成 `${sid}:${Date.now()}`（每帧唯一）→ 每个
// token 弹一条「等你回答」。实测单会话 1583 条，shell.log 里 77% 是通知流量、
// 40% 投递失败（主 run loop 被自己制造的风暴打到 is_waiting()=false）。
//
// notifyHub.test.ts 那类纯函数测试原理上抓不到这种结构性错误——必须把真实事件
// 喂进真实的 handle() reducer，数 notifyNative 的调用次数。两个方向都覆盖：
//   ① 非提问事件零通知（防复发）；
//   ② 真挂起问题仍然弹（防修过头把合法通知一起杀了）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// vi.mock 会被提升到 import 之前，工厂里不能直接引用顶层 const —— 用 hoisted。
// 参数签名要显式带上，否则 mock.calls[0][0] 推断成 never，取 payload 会 TS2493。
const { notifyNative } = vi.hoisted(() => ({
  notifyNative: vi.fn(async (_n: unknown) => true),
}));

vi.mock("@/lib/desktop", async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>();
  return { ...actual, notifyNative };
});

import { handle } from "@/components/chat/engine/handle";
import type { EngineCtx } from "@/components/chat/engine/ctx";

// 最小可跑通的 EngineCtx：只填 delta / tool 系 / user.question 三条路径真正读到
// 的字段（handle 顶部的 compaction 收尾 + adoptOrphanStream + ensureLive +
// mutateLive + maybeNotifyAskUser）。其余字段本测试路径不会触碰，整体 cast。
function makeCtx(overrides: Record<string, unknown> = {}): EngineCtx {
  const ref = <T,>(v: T) => ({ current: v });
  const base = {
    i18nRef: ref({
      tr: (k: string) => k,
      evText: (_e: unknown, f: string) => f,
    }),
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
    g: { sessions: [{ id: "s1", title: "测试会话" }] },
    syncDisplay: () => {},
  };
  return { ...base, ...overrides } as unknown as EngineCtx;
}

/** isWatching 的三条件里，本文件只需要操纵前两个（第三个走 ctx.activeSidRef）。
 *  默认「没在看」——通知闸门才会打开，正向用例才测得到东西。 */
function setAttention(opts: { visible?: boolean; path?: string } = {}) {
  vi.stubGlobal("document", {
    visibilityState: opts.visible ? "visible" : "hidden",
  });
  vi.stubGlobal("window", { location: { pathname: opts.path ?? "/" } });
}

type Row = { blocks: Array<{ kind: string; text?: string }> };
function blocksOf(ctx: EngineCtx, sid: string): string[] {
  return ((ctx.storeRef.current[sid] ?? []) as unknown as Row[]).flatMap((m) =>
    m.blocks.map((b) => b.kind),
  );
}

describe("handle() 的 OS 通知边界（P0-1 回归）", () => {
  beforeEach(() => {
    notifyNative.mockClear();
    setAttention({ visible: false });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // ── ① 非提问事件：一条通知都不许发 ──────────────────────────────────────
  // 这五个事件与 user.question 共用（或 fall through 进）同一段 case 体，
  // 是本次事故的载体。表驱动逐个点名，将来再往共享体里塞副作用会立刻红。
  const NON_QUESTION_EVENTS: Array<{ event: string; extra: Record<string, unknown> }> = [
    { event: "token.delta", extra: { content: "你" } },
    { event: "thinking.delta", extra: { content: "想一下" } },
    { event: "tool.start", extra: { id: "call_1", name: "bash" } },
    { event: "tool.args", extra: { id: "call_1", preview: "ls -la" } },
    { event: "tool.end", extra: { id: "call_1", content: "ok" } },
  ];

  it.each(NON_QUESTION_EVENTS)("$event 连发 50 帧 → 零 OS 通知", ({ event, extra }) => {
    const ctx = makeCtx();
    for (let i = 0; i < 50; i++) handle(ctx, "s1", { event, ...extra });
    expect(notifyNative).not.toHaveBeenCalled();
  });

  it("一整轮混合流（400 delta + 3 tool 帧）→ 零 OS 通知", () => {
    const ctx = makeCtx();
    for (let i = 0; i < 200; i++) handle(ctx, "s1", { event: "token.delta", content: "x" });
    handle(ctx, "s1", { event: "tool.start", id: "c1", name: "bash" });
    handle(ctx, "s1", { event: "tool.args", id: "c1", preview: "ls" });
    handle(ctx, "s1", { event: "tool.end", id: "c1", content: "ok" });
    for (let i = 0; i < 200; i++) handle(ctx, "s1", { event: "thinking.delta", content: "y" });
    expect(notifyNative).not.toHaveBeenCalled();
  });

  // 上面几个用例在同一毫秒内连发，旧实现的 `${sid}:${Date.now()}` 去重键会碰撞，
  // 所以只暴露出个位数调用。真实流是按 33ms 合帧节奏跨毫秒到达的（runtime
  // delta_coalesce），每帧拿到一把新钥匙 → 一帧一条通知。这里推进假时钟复现
  // 生产量级：修复前 50 帧 = 50 条，修复后 = 0 条。
  it("delta 按 33ms 合帧节奏跨毫秒到达 → 仍零 OS 通知（复现风暴量级）", () => {
    vi.useFakeTimers();
    try {
      const ctx = makeCtx();
      for (let i = 0; i < 50; i++) {
        vi.setSystemTime(new Date(1_700_000_000_000 + i * 33));
        handle(ctx, "s1", { event: "token.delta", content: "x" });
      }
      expect(notifyNative).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  // 无 id 是退化情形（ask_tools.py:359 的 tool_call_id 理论上总由 LangChain
  // 注入）。旧兜底 `${sid}:${Date.now()}` 让每帧拿到新钥匙 → 风暴；直接静默又
  // 会漏掉真挂起的问题。现改为「会话+问题文本」派生的稳定键：同一问题重放仍
  // 去重，不同问题各弹一条。
  it("无 id 的 user.question 连发 20 次同一问题 → 只弹一条（不洪水、也不静默）", () => {
    const ctx = makeCtx();
    for (let i = 0; i < 20; i++) {
      handle(ctx, "s1", { event: "user.question", question: "选哪个方案？" });
    }
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("无 id 但问题文本不同 → 各弹一条", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { event: "user.question", question: "问题甲" });
    handle(ctx, "s1", { event: "user.question", question: "问题乙" });
    expect(notifyNative).toHaveBeenCalledTimes(2);
  });

  // ── ② 真挂起问题：必须照弹（防修过头）──────────────────────────────────
  it("user.question 带 id → 恰好一条通知，标题带会话名", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { event: "user.question", id: "q_pos_1", question: "选哪个？" });
    expect(notifyNative).toHaveBeenCalledTimes(1);
    const payload = notifyNative.mock.calls[0][0] as {
      kind: string;
      id: string;
      title: string;
      body: string;
    };
    expect(payload).toMatchObject({ kind: "session", id: "s1" });
    expect(payload.title).toContain("测试会话");
    expect(payload.body).toBe("notify.askPending"); // tr 桩原样回键名
  });

  it("同一问题重放（WS 重连重发 parked 问题）不重复弹", () => {
    const ctx = makeCtx();
    for (let i = 0; i < 3; i++) {
      handle(ctx, "s1", { event: "user.question", id: "q_pos_2", question: "选哪个？" });
    }
    expect(notifyNative).toHaveBeenCalledTimes(1);
  });

  it("不同问题各弹一条", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { event: "user.question", id: "q_pos_3a" });
    handle(ctx, "s1", { event: "user.question", id: "q_pos_3b" });
    expect(notifyNative).toHaveBeenCalledTimes(2);
  });

  it("正盯着该会话时不弹（watching 闸门未被拆坏）", () => {
    setAttention({ visible: true, path: "/" });
    const ctx = makeCtx({ activeSidRef: { current: "s1" } });
    handle(ctx, "s1", { event: "user.question", id: "q_pos_4" });
    expect(notifyNative).not.toHaveBeenCalled();
  });

  // ── ③ 渲染行为未被拆 case 拆坏 ─────────────────────────────────────────
  it("user.question 仍渲染出 question 卡片", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { event: "user.question", id: "q_render", question: "选哪个？" });
    expect(blocksOf(ctx, "s1")).toContain("question");
  });

  it("delta 仍追加文本、tool.* 仍追加工具块（fall through 目标未变）", () => {
    const ctx = makeCtx();
    handle(ctx, "s1", { event: "token.delta", content: "你好" });
    handle(ctx, "s1", { event: "tool.start", id: "c9", name: "bash" });
    const kinds = blocksOf(ctx, "s1");
    expect(kinds).toContain("text");
    expect(kinds).toContain("tool");
  });
});
