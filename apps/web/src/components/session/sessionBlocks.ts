/**
 * sessionStream 基座（纯逻辑层）—— pin 浮窗 / Studio 开发抽屉共享的
 * 「单会话 WS 流」reducer/工厂/映射。重构批次 3-2（绞杀者）：三复制体
 * （m=useChatStreamEngine 多会话形态、pm=PinStream、dm=DevSessionDrawer）
 * fork 自同一骨架（批次 0 实测 574 行逐字相同），本模块吸收 pm/dm 子集；
 * m 是不同产品面仍留在原处。WS 生命周期见 useSessionSocket，消息环见
 * useSessionTurn。
 *
 * 纪律：本文件不 import React —— vitest node 环境直测；消费方差异（文案/
 * 渲染/附件/steer/team 联动）留在各自组件经 useSessionTurn 回调注入。
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

/** 工具「无结果」噪音过滤（与 ChatStream 同款白名单）。 */
export const EMPTY_TOOL_RESULT_RE =
  /^\s*(\(no matches\)|\(no files found\)|\(empty\)|no results|no files matched|no matches found|no files found|\(nothing found\))\s*$/i;

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

/** Streaming-event → block-list reducer（Pin/Dev 抽屉的 13 事件子集，
 *  Pin 超集：image.emit 一并收敛——Dev 抽屉此前静默丢弃产图事件，接入
 *  共享层后 dev agent 的 image 产物能落块渲染，行为见批次报告）。
 *  与 streamCore.applyBlock（主窗口完整版）语义对齐但刻意保持子集：
 *  浮窗/抽屉不渲染 usage / goal / plan 等 widget。 */
export function applyBlock(blocks: Block[], ev: SessionFrame): Block[] {
  const last = blocks[blocks.length - 1];
  switch (ev.event) {
    case "token.delta": {
      const t = (ev.content as string) || "";
      if (last && last.kind === "text") {
        const next = blocks.slice();
        next[next.length - 1] = { kind: "text", text: last.text + t };
        return next;
      }
      return [...blocks, { kind: "text", text: t }];
    }
    case "thinking.delta": {
      const t = (ev.content as string) || "";
      if (last && last.kind === "thinking") {
        const next = blocks.slice();
        next[next.length - 1] = { kind: "thinking", text: last.text + t };
        return next;
      }
      return [...blocks, { kind: "thinking", text: t }];
    }
    case "tool.start":
      return [
        ...blocks,
        {
          kind: "tool",
          id: ev.id as string | undefined,
          name: ev.name as string,
          content: "…",
          pending: true,
          argsPreview: (ev.preview as string) || undefined,
        },
      ];
    case "tool.args": {
      // 把工具实参预览（如 bash 命令）挂到 pending 气泡：用户看得见在跑什么。
      const id = ev.id as string | undefined;
      const preview = ev.preview as string;
      if (!id || !preview) return blocks;
      let matched = false;
      return blocks.map((b) => {
        if (b.kind !== "tool") return b;
        if (!matched && b.id === id) {
          matched = true;
          return { ...b, argsPreview: preview };
        }
        return b;
      });
    }
    case "tool.end": {
      const id = ev.id as string | undefined;
      const name = ev.name as string | undefined;
      const content = ev.content as string;
      if (EMPTY_TOOL_RESULT_RE.test(content)) {
        return blocks.filter((b) => {
          if (b.kind !== "tool") return true;
          const matches = id ? b.id === id : name ? b.name === name : b.pending;
          return !matches;
        });
      }
      let found = false;
      return blocks.map((b) => {
        if (b.kind !== "tool") return b;
        const matches = !found && (id ? b.id === id : name ? b.name === name : b.pending);
        if (matches) {
          found = true;
          return { ...b, content, pending: false };
        }
        return b;
      });
    }
    case "image.emit":
      return [
        ...blocks,
        {
          kind: "image",
          fileId: ev.file_id as string,
          name: ev.name as string | undefined,
          mtime: ev.mtime as number | undefined,
        },
      ];
    default:
      return blocks;
  }
}

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
