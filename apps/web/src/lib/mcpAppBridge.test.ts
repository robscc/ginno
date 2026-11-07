import { describe, expect, it, vi } from "vitest";
import { McpAppBridge, type McpAppBridgeDeps, type McpCallResult } from "./mcpAppBridge";

/** fake clock 模式（参照 notifyHub.test.ts）：时间注入，测试不依赖真实 timer。 */
function makeBridge(over: Partial<McpAppBridgeDeps> = {}) {
  const posted: Array<Record<string, unknown>> = [];
  const warns: string[] = [];
  const logs: unknown[][] = [];
  const clock = { t: 0, timers: [] as Array<{ at: number; fn: () => void; cancelled: boolean }> };
  const schedule = (fn: () => void, ms: number) => {
    const e = { at: clock.t + ms, fn, cancelled: false };
    clock.timers.push(e);
    return () => {
      e.cancelled = true;
    };
  };
  const iframe = { id: "iframe-window" }; // fake contentWindow（source 比对用）
  // over.callMcp 提前取出：返回的 h.callMcp 必须与 bridge 实际使用的是同一个
  // spy（否则断言看默认 spy，永远 0 次调用）
  const callMcp =
    over.callMcp ??
    vi.fn(async () => ({ content: ["ok"], structured: { done: true } }));
  const bridge = new McpAppBridge({
    targetWindow: iframe,
    postToApp: (m) => posted.push(m as Record<string, unknown>),
    callMcp,
    now: () => clock.t,
    schedule,
    warn: (m: string) => warns.push(m),
    log: (...a: unknown[]) => logs.push(a),
    minGapMs: 300,
    timeoutMs: 10_000,
    ...over,
  });
  const advance = (ms: number) => {
    clock.t += ms;
    for (const e of [...clock.timers]) {
      if (!e.cancelled && e.at <= clock.t) {
        e.cancelled = true;
        e.fn();
      }
    }
  };
  // drain 是 async（多个 await 点），flush 推空 microtask 队列
  const flush = async () => {
    for (let i = 0; i < 20; i++) await Promise.resolve();
  };
  const msg = (data: unknown, source: unknown = iframe) =>
    bridge.handleMessage({ source, data });
  return { bridge, posted, warns, logs, callMcp, advance, flush, msg, iframe };
}

const responses = (posted: Array<Record<string, unknown>>) =>
  posted.filter((m) => m.id !== undefined);
const dataPushes = (posted: Array<Record<string, unknown>>) =>
  posted.filter((m) => m.method === "ui/notifications/tool-result");

describe("McpAppBridge 来源校验与白名单", () => {
  it("event.source 不是目标 iframe：全部忽略", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 1, method: "ui/initialize" }, { id: "other-window" });
    h.msg({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "t" } }, "not-window");
    await h.flush();
    expect(h.posted).toEqual([]);
    expect(h.callMcp).not.toHaveBeenCalled();
  });

  it("非对象 data / 无 method：静默忽略", async () => {
    const h = makeBridge();
    h.msg("pong");
    h.msg({ jsonrpc: "2.0", id: 3, result: {} }); // response 形态
    await h.flush();
    expect(h.posted).toEqual([]);
    expect(h.warns).toEqual([]);
  });

  it("ui/initialize request → 应答官方 McpUiInitializeResult 结构（hostInfo/hostCapabilities，版本透传）", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 7, method: "ui/initialize",
            params: { protocolVersion: "2025-11-21", appCapabilities: {}, appInfo: { name: "demo", version: "0" } } });
    await h.flush();
    expect(responses(h.posted)).toEqual([
      { jsonrpc: "2.0", id: 7, result: { protocolVersion: "2025-11-21", hostInfo: { name: "ginno", title: "Ginno", version: "0.1.0" }, hostCapabilities: { serverTools: {} } } },
    ]);
  });

  it("ui/notifications/initialized 通知 → 容忍不回应；未知带 id 的 request → 回 -32601 防 app await 挂起", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    h.msg({ jsonrpc: "2.0", id: 9, method: "ui/open-link", params: { url: "https://x" } });
    await h.flush();
    expect(h.posted).toEqual([
      { jsonrpc: "2.0", id: 9, error: { code: -32601, message: "method not found: ui/open-link" } },
    ]);
  });

  it("ui/initialize notification（无 id）→ 不回应答", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", method: "ui/initialize" });
    await h.flush();
    expect(h.posted).toEqual([]);
  });

  it("未知 method：warn + 回 -32601（防官方 App 类 await 永挂），不透传", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 9, method: "window/open", params: { url: "https://evil" } });
    h.msg({ jsonrpc: "2.0", method: "mcp/client-no-id", params: {} }); // 无 id 通知：不回
    await h.flush();
    expect(h.posted).toEqual([
      { jsonrpc: "2.0", id: 9, error: { code: -32601, message: "method not found: window/open" } },
    ]);
    expect(h.callMcp).not.toHaveBeenCalled();
    expect(h.warns).toHaveLength(2);
  });

  it("logging/* 只打 console，不回推不转发", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", method: "logging/message", params: { level: "info", data: "hi" } });
    await h.flush();
    expect(h.posted).toEqual([]);
    expect(h.warns).toEqual([]);
    expect(h.logs).toHaveLength(1);
  });

  it("tools/call 缺 params.name：丢弃 + warn", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 11, method: "tools/call", params: { arguments: {} } });
    await h.flush();
    expect(h.callMcp).not.toHaveBeenCalled();
    expect(h.warns).toHaveLength(1);
  });

  it("dispose 后消息全部丢弃", async () => {
    const h = makeBridge();
    h.bridge.dispose();
    h.msg({ jsonrpc: "2.0", id: 12, method: "ui/initialize" });
    await h.flush();
    expect(h.posted).toEqual([]);
  });
});

describe("McpAppBridge tools/call 数据面", () => {
  it("成功：JSON-RPC result + ui/data 推送 content/structured", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 21, method: "tools/call", params: { name: "draw", arguments: { x: 1 } } });
    await h.flush();
    expect(h.callMcp).toHaveBeenCalledWith("draw", { x: 1 });
    const rs = responses(h.posted);
    expect(rs).toEqual([
      { jsonrpc: "2.0", id: 21, result: { content: ["ok"], structured: { done: true } } },
    ]);
    const pushes = dataPushes(h.posted);
    expect(pushes).toHaveLength(1);
    expect(pushes[0].params).toEqual({ content: ["ok"], structuredContent: { done: true } });
  });

  it("403/拒绝：错误文本经 ui/data error 字段推给 app，并回 JSON-RPC error", async () => {
    const h = makeBridge({
      callMcp: vi.fn(async () => {
        throw new Error('Tool call rejected by the permission policy (decision: ask)');
      }),
    });
    h.msg({ jsonrpc: "2.0", id: 22, method: "tools/call", params: { name: "denied" } });
    await h.flush();
    const rs = responses(h.posted);
    expect(rs[0].error).toMatchObject({ code: -32000 });
    expect(String((rs[0].error as { message?: string })?.message)).toContain("rejected");
    const pushes = dataPushes(h.posted);
    expect(pushes[0].params).toMatchObject({ isError: true, content: [expect.objectContaining({ type: "text" })] });
    expect(JSON.stringify(pushes[0].params)).toContain("rejected");
  });

  it("超时：10s 未 resolve → 超时错误回推，且不杀 callMcp promise", async () => {
    const h = makeBridge({
      callMcp: vi.fn(() => new Promise<McpCallResult>(() => {})), // 永不 settle
    });
    h.msg({ jsonrpc: "2.0", id: 23, method: "tools/call", params: { name: "slow" } });
    await h.flush();
    expect(responses(h.posted)).toEqual([]); // 未超时前无应答
    h.advance(10_000);
    await h.flush();
    const rs = responses(h.posted);
    expect((rs[0].error as { message?: string })?.message).toContain("timed out");
    expect(dataPushes(h.posted)[0].params).toMatchObject({ isError: true });
    expect(JSON.stringify(dataPushes(h.posted)[0].params)).toContain("timed out");
  });

  it("notification 形态的 tools/call（无 id）：只推 ui/data，不回 response", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", method: "tools/call", params: { name: "fire" } });
    await h.flush();
    expect(responses(h.posted)).toEqual([]);
    expect(dataPushes(h.posted)).toHaveLength(1);
  });
});

describe("McpAppBridge 节流（串行 + 300ms 最小间隔）", () => {
  it("首次立即执行；紧随的第二次至少等待 minGap", async () => {
    const h = makeBridge();
    h.msg({ jsonrpc: "2.0", id: 31, method: "tools/call", params: { name: "a" } });
    await h.flush();
    expect(responses(h.posted)).toHaveLength(1); // 首次不延迟

    h.msg({ jsonrpc: "2.0", id: 32, method: "tools/call", params: { name: "b" } });
    await h.flush();
    expect(responses(h.posted)).toHaveLength(1); // 间隔未到：b 未发出
    expect(h.callMcp).toHaveBeenCalledTimes(1);

    h.advance(299);
    await h.flush();
    expect(responses(h.posted)).toHaveLength(1); // 还差 1ms
    h.advance(1);
    await h.flush();
    expect(responses(h.posted)).toHaveLength(2); // 300ms 整：b 发出
    expect(h.callMcp).toHaveBeenNthCalledWith(2, "b", {});
  });

  it("串行：前一个未完成时后一个排队，不并发", async () => {
    let release!: (v: { content: string[] }) => void;
    const h = makeBridge({
      callMcp: vi.fn(
        () =>
          new Promise<{ content: string[] }>((r) => {
            release = r;
          }),
      ),
    });
    h.msg({ jsonrpc: "2.0", id: 41, method: "tools/call", params: { name: "first" } });
    h.msg({ jsonrpc: "2.0", id: 42, method: "tools/call", params: { name: "second" } });
    await h.flush();
    expect(h.callMcp).toHaveBeenCalledTimes(1); // 第二个在队列里
    release({ content: ["first-done"] });
    await h.flush(); // first 完成 + second 进入 minGap 等待
    h.advance(300); // 越过 minGap
    await h.flush();
    expect(h.callMcp).toHaveBeenCalledTimes(2); // 排队的第二个此刻才发
    release({ content: ["second-done"] }); // release 已指向第二个 promise
    await h.flush();
    expect(dataPushes(h.posted).map((p) => (p.params as { content: Array<{ text?: string }> }).content?.[0])).toEqual([
      "first-done",
      "second-done",
    ]);
  });
});
