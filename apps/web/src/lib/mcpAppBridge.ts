/**
 * 与单个沙箱 iframe 绑定的 postMessage JSON-RPC 桥（MCP Apps 方言，
 * docs/mcp-apps-design.md §2 web 侧）。纯逻辑：DOM/网络依赖全部注入，
 * node vitest 可测（lib/mcpAppBridge.test.ts）。
 *
 * 安全底线（goal 约束 7，不可妥协）：
 * - 入站：event.source 必须严格 === 目标 iframe.contentWindow。沙箱 iframe
 *   （无 allow-same-origin）是不透明源，origin 恒为 "null"，source 比对是
 *   唯一可靠的来源校验。
 * - 入站 method 白名单：ui/initialize、tools/call、logging/*（后者只打
 *   console）。未知 method 丢弃 + console.warn，绝不透传。
 * - 出站：只发 ui/initialize 应答与 ui/data 推送；targetOrigin 用 "*"
 *   （不透明源无法收窄）——payload 不含任何凭证，泄露面为零。
 * - 节流：tools/call 串行队列 + 最小间隔（默认 300ms）——密集回调的 app
 *   （budget-allocator 类）打不爆 server。
 * - 超时：单次 tools/call 默认 10s；超时按错误回推（JSON-RPC error +
 *   ui/data error 字段）。
 */

export type JsonRpcId = number | string;

export type JsonRpcMessage = {
  jsonrpc?: string;
  id?: JsonRpcId;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
};

export type McpCallResult = { content: string[]; structured?: unknown };

export interface McpAppBridgeDeps {
  /** 目标 iframe 的 contentWindow（入站 source 严格比对）。 */
  targetWindow: unknown;
  /** host → app：postMessage(msg, "*")（由持有 iframe 的组件注入）。 */
  postToApp: (msg: JsonRpcMessage) => void;
  /** POST /api/mcp/call（server 名由组件闭包注入；tool 用 app 上报的原始名）。 */
  callMcp: (tool: string, args: Record<string, unknown>) => Promise<McpCallResult>;
  /** 节流最小间隔，默认 300ms。 */
  minGapMs?: number;
  /** 单次 call 超时，默认 10s。 */
  timeoutMs?: number;
  now?: () => number;
  /** 定时器注入（测试用 fake clock）；返回 cancel。 */
  schedule?: (fn: () => void, ms: number) => () => void;
  warn?: (msg: string, ...rest: unknown[]) => void;
  log?: (...rest: unknown[]) => void;
}

interface _QueueItem {
  tool: string;
  args: Record<string, unknown>;
  id?: JsonRpcId;
}

// 入站 method 白名单 = handleMessage 里显式分支的三个前缀：
// ui/initialize、tools/call、logging/*（未知一律丢弃 + warn）。

function _isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export class McpAppBridge {
  private readonly deps: Required<Pick<McpAppBridgeDeps, "targetWindow" | "postToApp" | "callMcp">> &
    McpAppBridgeDeps;
  private readonly minGapMs: number;
  private readonly timeoutMs: number;
  private readonly now: () => number;
  private readonly schedule: (fn: () => void, ms: number) => () => void;
  private queue: _QueueItem[] = [];
  private draining = false;
  // -Infinity：首次调用不节流（now() - lastEndAt 恒超 minGap）
  private lastEndAt = Number.NEGATIVE_INFINITY;
  private disposed = false;

  constructor(deps: McpAppBridgeDeps) {
    this.deps = deps;
    this.minGapMs = deps.minGapMs ?? 300;
    this.timeoutMs = deps.timeoutMs ?? 10_000;
    this.now = deps.now ?? (() => Date.now());
    this.schedule =
      deps.schedule ??
      ((fn, ms) => {
        const h = setTimeout(fn, ms);
        return () => clearTimeout(h);
      });
  }

  /** window "message" 监听器里调用（组件已绑定生命周期）。 */
  handleMessage(ev: { source: unknown; data: unknown }): void {
    if (this.disposed) return;
    // 严格来源校验：只认目标 iframe（其它 iframe / window 自身的消息全忽略）
    if (ev.source !== this.deps.targetWindow) return;
    const msg = ev.data;
    if (!_isPlainObject(msg)) return;
    const method = msg.method;
    if (typeof method !== "string") return; // JSON-RPC response/非 request 形态：静默
    const id = msg.id as JsonRpcId | undefined;
    if (method === "ui/initialize") {
      this.replyInitialize(id, msg.params);
      return;
    }
    if (method === "ui/notifications/initialized") {
      // 官方握手第二拍：app 完成通知，host 无需动作
      this.deps.log?.("mcpApp: initialized notification received");
      return;
    }
    if (method === "tools/call") {
      const params = _isPlainObject(msg.params) ? msg.params : {};
      const tool = typeof params.name === "string" ? params.name : "";
      if (!tool) {
        this.deps.warn?.("mcpAppBridge: tools/call without params.name, dropped");
        return;
      }
      const args = _isPlainObject(params.arguments)
        ? (params.arguments as Record<string, unknown>)
        : {};
      this.queue.push({ tool, args, id });
      void this.drain();
      return;
    }
    if (method.startsWith("logging/")) {
      this.deps.log?.("mcpApp:", method, msg.params);
      return;
    }
    this.deps.warn?.(`mcpAppBridge: dropped unknown method "${method}"`);
    // 官方 App 类对带 id 的 request 会 await 回应——未知 request 必须回
    // JSON-RPC error（而非静默丢弃），否则 app 侧 Promise 永挂。
    if (id !== undefined) {
      this.reply(id, undefined, { code: -32601, message: `method not found: ${method}` });
    }
  }

  /** host 主动推送（初始工具结果 + 后续更新）。官方 wire：notification
   * `ui/notifications/tool-result`，params = 标准 MCP CallToolResult
   * （{content: [...], isError?}）。接受调用方 {tool, content} 旧形态，
   * 规整为 content-only（App.ontoolresult 只读 content）。 */
  pushData(payload: Record<string, unknown>): void {
    if (this.disposed) return;
    // 规整为官方 CallToolResult content 块数组：裸字符串元素（如
    // McpAppCard 初始推送的 [data]）映射为 {type:"text"} 块——app 侧
    // SDK 读 content[i].text，裸字符串会让表格渲染成空（2026-11-11
    // trending 空表根因）。
    const raw = Array.isArray(payload.content)
      ? payload.content
      : [{ type: "text", text: String(payload.content ?? "") }];
    const content = raw.map((el) =>
      typeof el === "string"
        ? { type: "text", text: el }
        : (el as { type: string; text?: string }),
    );
    const params: Record<string, unknown> = { content };
    const structured =
      payload.structuredContent ?? (payload as { structured?: unknown }).structured;
    if (structured != null) params.structuredContent = structured;
    if (payload.isError === true) params.isError = true;
    if (typeof payload.error === "string") {
      // 错误回推：官方 CallToolResult 形态（isError + 文本 content）
      params.isError = true;
      params.content = [{ type: "text", text: payload.error }];
    }
    this.deps.postToApp({
      jsonrpc: "2.0",
      method: "ui/notifications/tool-result",
      params,
    });
  }

  dispose(): void {
    this.disposed = true;
    this.queue = [];
  }

  // ------------------------------------------------------------------ //

  private replyInitialize(id?: JsonRpcId, reqParams?: unknown): void {
    // notification 形态（无 id）不回应答；request 应答按官方 McpUiInitializeResultSchema：
    // { protocolVersion, hostInfo, hostCapabilities, hostContext? }——字段名与
    // 官方 ext-apps App 类的 zod 校验严格对齐（2026-11-07 协议联调实测；
    // 旧版回 {capabilities:[…]} 会导致 App.connect() 校验失败、页面卡「连接 host…」）。
    if (id === undefined) return;
    const reqVersion =
      _isPlainObject(reqParams) && typeof reqParams.protocolVersion === "string"
        ? reqParams.protocolVersion
        : "2025-11-21";
    this.deps.postToApp({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: reqVersion,
        hostInfo: { name: "ginno", title: "Ginno", version: "0.1.0" },
        hostCapabilities: { serverTools: {} },
        // hostContext 必填（App 类 zod 严格校验，2026-11-07 联调实测）；子字段
        // 全 optional——给主题即可，toolInfo 等按需后续补。
        hostContext: { theme: "dark" },
      },
    });
  }

  private reply(id: JsonRpcId | undefined, result: unknown, error?: { code: number; message: string }): void {
    if (id === undefined) return;
    const msg: JsonRpcMessage = { jsonrpc: "2.0", id };
    if (error) msg.error = error;
    else msg.result = result;
    this.deps.postToApp(msg);
  }

  private async drain(): Promise<void> {
    if (this.draining || this.disposed) return;
    this.draining = true;
    try {
      while (this.queue.length && !this.disposed) {
        const item = this.queue.shift()!;
        // 节流：距上次完成不足 minGap → 等待剩余时间（不阻塞其它消息处理）
        const wait = this.minGapMs - (this.now() - this.lastEndAt);
        if (wait > 0) await this.sleep(wait);
        if (this.disposed) return;
        await this.dispatch(item);
        this.lastEndAt = this.now();
      }
    } finally {
      this.draining = false;
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => this.schedule(resolve, ms));
  }

  private async dispatch(item: _QueueItem): Promise<void> {
    // no-op 初始化：赋值发生在 Promise 构造回调里，null 初始化会被 TS 控制流
    // narrow 成 never（TS2349）；no-op 哨兵保持 () => void 类型
    let cancelTimeout: () => void = () => {};
    const timeout = new Promise<"timeout">((resolve) => {
      cancelTimeout = this.schedule(() => resolve("timeout"), this.timeoutMs);
    });
    try {
      const result = await Promise.race([this.deps.callMcp(item.tool, item.args), timeout]);
      if (result === "timeout") {
        const message = `tools/call "${item.tool}" timed out after ${this.timeoutMs}ms`;
        this.reply(item.id, undefined, { code: -32000, message });
        this.pushData({ tool: item.tool, error: message });
        return;
      }
      this.reply(item.id, result);
      this.pushData({ tool: item.tool, content: result.content, structured: result.structured });
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      // 403 / server 错误：错误文本经 ui/data 推给 app（goal：带 error 字段）
      this.reply(item.id, undefined, { code: -32000, message });
      this.pushData({ tool: item.tool, error: message });
    } finally {
      cancelTimeout();
    }
  }
}
