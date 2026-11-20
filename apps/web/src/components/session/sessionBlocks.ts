/**
 * sessionStream 基座（纯逻辑层）—— pin 浮窗 / Studio 开发抽屉共享的
 * 「单会话 WS 流」reducer/工厂/映射。重构批次 3-2（绞杀者）：三复制体
 * （m=useChatStreamEngine 多会话形态、pm=PinStream、dm=DevSessionDrawer）
 * fork 自同一骨架（批次 0 实测 574 行逐字相同），本模块吸收 pm/dm 子集。
 * P1-4（本提交）：applyBlock 升级为主版超集（见下），pm/dm 全部收敛到
 * 本模块单实现——m 的真源在 streamCore 不动。WS 生命周期见 useSessionSocket，
 * 消息环见 useSessionTurn。
 *
 * 纪律：本文件不 import React —— vitest node 环境直测（applyBlock 的
 * re-export 目标 streamCore 也只有 `import type`，无运行时依赖）；消费方
 * 差异（文案/渲染/附件/steer/team 联动）留在各自组件经 useSessionTurn
 * 回调注入。
 */

import type { Block } from "@/components/chat/blocks";

/** 两消费方消息类型的共同基座（DrawerMsg = 基座；PinMsg 额外带
 *  teamReply / retryText 扩展字段，泛型收敛在 useSessionTurn）。 */
export interface SessionMsg {
  id: string;
  /** "system" = 居中上下文行（压缩/团队回执），不是气泡。 */
  role: "user" | "assistant" | "system";
  blocks: Block[];
  turnId?: string;
  /** Turn-error 卡（渲染为红色）。 */
  error?: boolean;
}

export interface PermissionPrompt {
  tool: string;
  args: unknown;
}

/** WS 流帧（runtime session socket 事件；frame_session 归属守卫见
 *  useSessionSocket）。 */
export interface SessionFrame {
  event: string;
  [k: string]: unknown;
}

/** /history 单条消息（服务端已映射为 blocks）。 */
export interface SessionHistoryMessage {
  id?: string;
  role: "user" | "assistant";
  blocks: Block[];
  turnId?: string;
}

/** /history 响应（messages 已服务端映射为 blocks）。 */
export interface SessionHistoryResponse {
  messages?: SessionHistoryMessage[];
  last_error?: {
    message?: string;
    turn_id?: string;
    i18n_key?: string;
    params?: Record<string, string | number>;
  } | null;
}

/** Turn id（回服务端的 turn_id；crypto 优先，SSR/旧 webview 回退时间戳）。 */
export const newTurnId = (): string =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `t-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

/** 消息 id 工厂：前缀区分复制体来源（pm/dm），调试时一眼可辨。 */
export function makeMid(prefix: string): () => string {
  let n = 0;
  return () => `${prefix}${++n}`;
}

/** 工具「无结果」噪音过滤（与 ChatStream 同款白名单）。真源在
 *  streamCore.isEmptyToolResult（同正则）——主版 applyBlock 的 tool.end
 *  空结果过滤直接复用它，这里 re-export 供消费方/测试引用同一实现。 */
export { isEmptyToolResult } from "@/components/chat/streamCore";

/** 单会话 WS 生命周期四常数（三复制体逐字钉死的行为，批次 0 对齐基线）：
 *  断线 3s 重连 / 20s 心跳 ping / 45s 静默强断（10s 巡检）/ 探针 6s 兜底。
 *  放本文件（而非 useSessionSocket.ts）是为了让 node 环境测试能直接钉住
 *  数值——socket 模块本身 import 了 runtime/i18n 链。 */
export const SOCKET_TIMING = {
  RECONNECT_MS: 3000,
  PING_MS: 20000,
  SILENCE_CLOSE_MS: 45000,
  WATCH_TICK_MS: 10000,
  RECONCILE_FALLBACK_MS: 6000,
} as const;

/** Streaming-event → block-list reducer（Pin/Dev 抽屉共享，P1-4 起为主版
 *  超集）：真源是主窗口 streamCore.applyBlock，此处 re-export——Pin（经
 *  usePinSessionLoop）与 Dev 抽屉（经 useSessionTurn）与主窗口三端同实现。
 *  #240 的三个修复语义（2026-11-15 tool.end 定位算法：有 id 精确匹配不限
 *  pending 保重连重放幂等、无 id 只认 pending 从后往前取最近同名；空结果
 *  只删目标块；无配对块 append 兜底不静默丢结果）与 appendStreamDelta 交叉
 *  增量合并（2026-10-01）全部继承；user.question / widget / workflow / ref /
 *  llm_call 等主版块能力也随真源就位。
 *
 * 消费方为什么不需要过滤主版的新块类型：useSessionTurn / usePinSessionLoop
 * 的 handle() 只把 6 种流事件（token.delta / thinking.delta / tool.start /
 * tool.args / tool.end / image.emit——主版事件集的严格子集）喂进 mutateLive；
 * 主版 reducer 会为其建块的 user.question / widget.emit / workflow.emit /
 * ref.emit / llm.request / llm.response 在两消费方一律走 default 分支
 * （onExtraEvent，floating-window-design.md §2.4 刻意忽略：浮窗/抽屉不渲染
 * usage / goal / plan 等 widget），这些 case 在该路径不可达。因此这 6 事件上
 * 主版可能产出的块类型只有 text / thinking / tool / image——InnerBlocks 均
 * 已渲染，无兼容缺口。（tool.end 的 foldQuestion 前置检查同理：消费方
 * blocks 里永无 question 块，qi 恒为 -1。）
 *
 * 与子集版（批次 3-2，本提交前）的可见差异，均为主版修复语义：
 * ① tool.end 无配对块时 append 已完成工具块（旧：静默丢结果）；
 * ② tool.end 无 id 时 name 兜底只认 pending、从后往前取最近同名（旧：
 *    不限 pending 从头扫取最早，同名工具块结果错位）；
 * ③ name-only 空结果只删目标块（旧：filter 全删一个 turn 内所有同名块）；
 * ④ delta 交叉合并：text/thinking 增量并回「最近一个非流式块之后」的最近
 *    同 kind 块，工具气泡/卡片仍是边界（旧：只看末尾同 kind，交叉流式时
 *    实时视图碎成夹在正文中的独立小卡片）；
 * ⑤ tool.start 不再读 preview 字段——服务端 tool.start 只发 {name, id}
 *    （api/stream/stages/toolloop.py 等），实参预览专用 tool.args 事件；
 *    子集版的 argsPreview: ev.preview 是永不触发的死代码。 */
export { applyBlock } from "@/components/chat/streamCore";

/** closeLive(finalizeTools) 的内核：stop/error 路径把仍在 pending 的工具
 *  气泡强制收尾（服务端不会再发它们的 tool.end），“…” 占位换成中断文案。 */
export function finalizePendingTools(blocks: Block[], interruptedLabel: string): Block[] {
  return blocks.map((b) =>
    b.kind === "tool" && b.pending
      ? { ...b, pending: false, content: b.content === "…" ? interruptedLabel : b.content }
      : b,
  );
}

export interface MapHistoryOpts<Msg extends SessionMsg> {
  /** last_error 带 i18n key 时落地翻译（Pin 的 useEventI18nText 契约）；
   *  缺省英文 message 原样直显。 */
  errorText?: (
    le: NonNullable<SessionHistoryResponse["last_error"]>,
    fallback: string,
  ) => string;
  /** 历史条目装饰（Pin：teamReply 透传）。 */
  decorate?: (raw: SessionHistoryMessage, msg: Msg) => Msg;
  /** 错误卡附 retryText（Pin 的重试按钮来源）。 */
  withRetryText?: boolean;
}

/** /history → 消息数组（两消费方 mapHistory 的参数化合并）。 */
export function mapHistoryToMsgs<Msg extends SessionMsg>(
  res: SessionHistoryResponse,
  mid: () => string,
  opts: MapHistoryOpts<Msg> = {},
): Msg[] {
  const msgs: Msg[] = (res.messages ?? []).map((m) => {
    const base: Msg = {
      id: m.id ?? mid(),
      role: m.role === "user" ? "user" : "assistant",
      blocks: m.blocks ?? [],
      turnId: m.turnId,
    } as Msg;
    return opts.decorate ? opts.decorate(m, base) : base;
  });
  // 持久化的 turn 失败：重开窗口后仍要浮出（与 ChatStream 的 mapHistory 同契约）。
  if (res.last_error?.message) {
    const lastUser = [...msgs].reverse().find((m) => m.role === "user");
    const textBlock = lastUser?.blocks.find((b) => b.kind === "text");
    const text = opts.errorText
      ? opts.errorText(res.last_error, res.last_error.message)
      : res.last_error.message;
    msgs.push({
      id: mid(),
      role: "assistant",
      error: true,
      blocks: [{ kind: "text", text }],
      ...(opts.withRetryText
        ? {
            retryText:
              textBlock && "text" in textBlock ? (textBlock as { text: string }).text : undefined,
          }
        : {}),
    } as Msg);
  }
  return msgs;
}
