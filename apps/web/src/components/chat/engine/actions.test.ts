// actions.retryFromCheckpoint 断线窗兜底（R3 巡检 Q3，2026-11-22）。
// 修复前「删卡」先于 socket 检查：断线窗口内点重试会静默删掉错误卡且
// 什么都不发生；send 抛错同样丢卡。这里直接调纯函数 + 假 ctx 断言：
// 断线保卡+可见提示、60s 内重连自动补发、超时保卡+错误反馈可重试、
// send 抛错保卡、正常路径帧发出后才删卡置忙。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { pushNotice } = vi.hoisted(() => ({ pushNotice: vi.fn() }));
vi.mock("@/lib/notice", () => ({ pushNotice }));
vi.mock("@/i18n/uiText", () => ({ uiText: (k: string) => k }));
vi.mock("@/lib/runtime", () => ({
  subagentPlanCancelFrame: vi.fn(),
  subagentPlanConfirmFrame: vi.fn(),
}));

import { retryFromCheckpoint } from "./actions";
import type { EngineCtx } from "./ctx";
import type { ChatMsg } from "../streamCore";

function mkCard(id = "card1"): ChatMsg {
  return {
    id,
    role: "assistant",
    blocks: [{ kind: "text", text: "boom" }],
    turnId: "t1",
    error: true,
    sendPayload: { text: "hi", images: [], files: [], mentions: [], agentId: null },
  } as unknown as ChatMsg;
}

function mkCtx(card: ChatMsg, sock: unknown = undefined) {
  const ctx = {
    curSessionIdRef: { current: "s1" },
    busyBySessionRef: { current: {} as Record<string, boolean> },
    compactStageRef: { current: {} },
    storeRef: { current: { s1: [card] } as Record<string, ChatMsg[]> },
    socketsRef: { current: { s1: sock } as Record<string, WebSocket> },
    syncDisplay: vi.fn(),
  };
  return ctx as unknown as EngineCtx;
}

const OPEN = 1;

function openSock(send: (frame: string) => void = () => {}) {
  return { readyState: OPEN, send: vi.fn(send) } as unknown as WebSocket;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  // CI 的 node 环境无全局 WebSocket（源码读 WebSocket.OPEN 常量比对
  // readyState）——stub 出常量即可，行为由假 socket 对象驱动。
  vi.stubGlobal("WebSocket", { OPEN: 1 });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("retryFromCheckpoint 断线窗兜底（Q3）", () => {
  // module 级 _ckptRetryWaiting 跨用例共享（模拟页面级单例），用例间用
  // 唯一卡 id 互不干扰；需要复点语义的用例在自己体内闭环。
  let CID = "card-0";

  beforeEach(() => {
    CID = `card-${crypto.randomUUID().slice(0, 8)}`;
  });
  it("断线：卡保留 + 可见等待提示 + 不置忙（修复前：卡被静默删掉）", () => {
    const ctx = mkCtx(mkCard(CID), undefined);
    retryFromCheckpoint(ctx, CID);
    expect(ctx.storeRef.current.s1).toHaveLength(1); // 卡还在
    expect(ctx.busyBySessionRef.current.s1).toBeFalsy();
    expect(pushNotice).toHaveBeenCalledWith(
      "warn",
      "net.resumeWaiting",
      expect.objectContaining({ dedupeKey: `ckpt-retry-wait:${CID}` }),
    );
  });

  it("等待期内重连：自动补发 resume 帧（原 turnId），帧发出后才删卡置忙", async () => {
    const ctx = mkCtx(mkCard(CID), undefined);
    retryFromCheckpoint(ctx, CID);
    // 重连成功（socket 替换为 OPEN——模拟自动重连换新 socket）
    const sock = openSock();
    ctx.socketsRef.current.s1 = sock;
    await vi.advanceTimersByTimeAsync(500);
    expect(sock.send).toHaveBeenCalledWith(JSON.stringify({ type: "retry_from_checkpoint", turn_id: "t1" }));
    expect(ctx.storeRef.current.s1).toHaveLength(0); // 帧发出后卡才删
    expect(ctx.busyBySessionRef.current.s1).toBe(true);
    expect(ctx.syncDisplay).toHaveBeenCalled();
  });

  it("重复点击去重：等待期内再点不重复起轮询/补发", async () => {
    const ctx = mkCtx(mkCard(CID), undefined);
    retryFromCheckpoint(ctx, CID);
    retryFromCheckpoint(ctx, CID); // 第二次点击应被忽略
    expect(pushNotice).toHaveBeenCalledTimes(1);
    const sock = openSock();
    ctx.socketsRef.current.s1 = sock;
    await vi.advanceTimersByTimeAsync(500);
    expect(sock.send).toHaveBeenCalledTimes(1);
  });

  it("60s 超时：卡保留 + 可见错误反馈，且可再次点击重新等待（可重试）", async () => {
    const ctx = mkCtx(mkCard(CID), undefined);
    retryFromCheckpoint(ctx, CID);
    await vi.advanceTimersByTimeAsync(61_000);
    expect(pushNotice).toHaveBeenCalledWith(
      "error",
      "net.resumeOffline",
      expect.objectContaining({ dedupeKey: `ckpt-retry-offline:${CID}` }),
    );
    expect(ctx.storeRef.current.s1).toHaveLength(1);
    expect(ctx.busyBySessionRef.current.s1).toBeFalsy();
    // 超时后 waiting 已清——再点能重新进入等待（卡是可重试入口）
    pushNotice.mockClear();
    retryFromCheckpoint(ctx, CID);
    expect(pushNotice).toHaveBeenCalledWith("warn", "net.resumeWaiting", expect.anything());
  });

  it("等待期结束但卡已被移除（刷新/切源）：不补发", async () => {
    const ctx = mkCtx(mkCard(CID), undefined);
    retryFromCheckpoint(ctx, CID);
    ctx.storeRef.current.s1 = []; // 等待期间卡没了
    const sock = openSock();
    ctx.socketsRef.current.s1 = sock;
    await vi.advanceTimersByTimeAsync(500);
    expect(sock.send).not.toHaveBeenCalled();
  });

  it("socket OPEN 但 send 抛错：卡保留，进入等待而非白删", () => {
    const bad = { readyState: OPEN, send: () => { throw new Error("closed"); } } as unknown as WebSocket;
    const ctx = mkCtx(mkCard(CID), bad);
    retryFromCheckpoint(ctx, CID);
    expect(ctx.storeRef.current.s1).toHaveLength(1); // 修复前这里卡已被删
    expect(pushNotice).toHaveBeenCalledWith("warn", "net.resumeWaiting", expect.anything());
  });

  it("正常路径：OPEN + send 成功 → 帧发出、卡删、置忙，无提示", () => {
    const sock = openSock();
    const ctx = mkCtx(mkCard(CID), sock);
    retryFromCheckpoint(ctx, CID);
    expect(sock.send).toHaveBeenCalledWith(JSON.stringify({ type: "retry_from_checkpoint", turn_id: "t1" }));
    expect(ctx.storeRef.current.s1).toHaveLength(0);
    expect(ctx.busyBySessionRef.current.s1).toBe(true);
    expect(pushNotice).not.toHaveBeenCalled();
  });

  it("非错误卡 / busy 会话 / 无会话：原守卫不变", () => {
    const ok = { ...mkCard(CID), error: false } as unknown as ChatMsg;
    const ctx = mkCtx(ok, openSock());
    retryFromCheckpoint(ctx, CID);
    expect(ctx.storeRef.current.s1).toHaveLength(1);
    expect(ctx.busyBySessionRef.current.s1).toBeUndefined();

    const busyCtx = mkCtx(mkCard(), openSock());
    busyCtx.busyBySessionRef.current.s1 = true;
    retryFromCheckpoint(busyCtx, CID);
    expect(busyCtx.storeRef.current.s1).toHaveLength(1);
  });
});
