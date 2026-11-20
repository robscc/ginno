// pinStreamCore — PinStream 的流事件/看门狗接入层（重构批次 3-1）。
//
// 背景：PinStream.tsx 曾按 floating-window-design.md 维护一份 applyBlock 的
// 「刻意副本」，但三处落后于主窗口（批次 0 重复度测量 P0——pin 用户仍在踩
// 主窗口已修的坑）：
//   ① tool.end 定位停在 2026-11-15 之前的旧算法：name 兜底不要求 pending、
//     map 从头扫取最早同名块（结果错位），name-only 空结果走 filter 全删
//     （一个 turn 内同名工具块被一次删光）；
//   ② delta 合块只在「末尾同 kind」才合并，thinking 与正文交叉流式时实时
//     视图碎成夹在正文中的独立小卡片（主版 2026-10-01 已修，见
//     streamCore.appendStreamDelta）；
//   ③ watchdog 只有 45s 静默关连接 + onopen 单次 probe，缺主版 fe0d06e
//     （a1618f7）的 believing-live 周期 turn_state 探针——收尾帧在 ws 未断
//     时丢失会永远卡在直播态。
//
// 修复方式 = 直调主版 streamCore.applyBlock + 把 watchdog / 探针回答决策抽成
// 本文件的纯函数（node 环境可单测，组件文件不再藏不可测的闭包逻辑）。
//
// 兼容性论证（为什么接入处**不需要**过滤主版可能产生的新块类型）：PinStream
// 的 handle() switch 只把 6 种流事件（token.delta / thinking.delta /
// tool.start / tool.args / tool.end / image.emit——主版事件集的严格子集）喂进
// pinApplyBlock；主版 reducer 会为其建块的 user.question / widget.emit /
// workflow.emit / ref.emit / llm.request / llm.response 在 pin 侧一律走
// default 分支丢弃（floating-window-design.md §2.4 刻意忽略），这些分支在 pin
// 路径上不可达。因此主版在这 6 事件上可能产出的块类型只有
// text / thinking / tool / image——InnerBlocks 均已渲染，无兼容缺口。
// （tool.end 的 foldQuestion 前置检查同理：pin 的 blocks 里永无 question 块，
//  qi 恒为 -1。）

import { applyBlock } from "@/components/chat/streamCore";
import type { Block } from "@/components/chat/blocks";

/** Pin 侧唯一的流事件 → block-list 归约入口（主版 applyBlock 直调）。
 *  直调即继承主版全部语义：2026-11-15 的 tool.end 定位（id 精确匹配不限
 *  pending 保重连重放幂等；无 id 只认 pending 且从后往前取最近同名）、
 *  空结果只删目标块、无配对块时 append 兜底不静默丢结果、以及
 *  appendStreamDelta 的交叉增量合并。 */
export function pinApplyBlock(
  blocks: Block[],
  ev: { event: string; [k: string]: unknown },
): Block[] {
  return applyBlock(blocks, ev);
}

/** 45s 无任何入站帧 → 主动断线换新连接（重连循环走 reconcile）。 */
export const PIN_SILENCE_CLOSE_MS = 45_000;

/** watchdog 单 tick 决策（主版 useChatStreamEngine watchTimer 的纯函数化）。
 *
 * - close：静默超 45s——ping 照常但服务端一帧未发，连接视为僵死。
 * - probe：believing-live/busy 时的周期 turn_state 探针（fe0d06e / a1618f7
 *   镜像）。收尾帧（message.end / tool.end / user.question）在 ws 未断时
 *   丢失的话，静默看门狗永远不触发（ping 有应答不算静默），前端就永远停在
 *   「pending 气泡 + 计时冻结」。探针回答走既有 turn.state handler：服务端
 *   说还在跑 → no-op（流照常续）；说不在跑 → reconcileFromHistory 从
 *   checkpoint 重建。与主版一致按 10s watchdog tick 直发（≈卡死上限一个
 *   探针周期的密度）。 */
export interface PinWatchdogDecision {
  close: boolean;
  probe: boolean;
}

export function pinWatchdogTick(opts: {
  now: number;
  lastSeen: number;
  believingLive: boolean;
  socketOpen: boolean;
}): PinWatchdogDecision {
  return {
    close: opts.now - opts.lastSeen > PIN_SILENCE_CLOSE_MS,
    probe: opts.believingLive && opts.socketOpen,
  };
}

/** turn.state 探针回答的处置决策（主版 engine/handle.ts turn.state 的
 *  纯函数化，含其 sendSeq>probeSeq 守卫）。
 *
 * - resume：服务端说 turn 还在跑——本 socket 继续吃广播流，no-op。
 * - stale：回答描述的是「服务端处理探针那一刻」的会话态；此后本端又发出过
 *   新 turn（invoke 的 sendSeq 晚于探针的 probeSeq）时，running=false 不
 *   能据此 reconcile——否则把刚发出的 turn 判死（新会话首条消息：socket
 *   由发送打开、onopen probe 在 t0、invoke 在 t0+13ms 是真实时序）。等流
 *   事件自愈：真正的 turn 必以 message.end / error 收口。
 * - idle：本端并不 believing-live——探针不该到达（防御：发出后、回答前
 *   turn 恰好收口），无事可做。
 * - reconcile：believing-live 且服务端说不在跑 → 从 checkpoint 重建并复位
 *   busy（断线期间 turn 已收口 / 收尾帧丢失两条路都从这里自愈）。 */
export type PinTurnStateAction = "resume" | "stale" | "idle" | "reconcile";

export function pinTurnStateAction(
  ev: { running?: unknown },
  ctx: { believingLive: boolean; sendSeq: number; probeSeq: number },
): PinTurnStateAction {
  if (ev.running) return "resume";
  if (ctx.sendSeq > ctx.probeSeq) return "stale";
  if (!ctx.believingLive) return "idle";
  return "reconcile";
}
