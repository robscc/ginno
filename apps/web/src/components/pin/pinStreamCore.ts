// pinStreamCore — PinStream 的看门狗/探针决策层（重构批次 3-1，P1-4 收窄）。
//
// 历史：PinStream.tsx 曾按 floating-window-design.md 维护一份 applyBlock 的
// 「刻意副本」，批次 3-1（#240）修复为直调主版 streamCore.applyBlock；
// P1-4 把共享层 sessionBlocks.applyBlock 升级为主版超集（re-export 同一
// 真源）后，pinApplyBlock 包装层删除——usePinSessionLoop 直接用共享层
// applyBlock，Pin/Dev/主窗口三端单实现。applyBlock 的事件白名单论证
// （为什么 pin 路径不会产出主版新块类型）随实现搬去了 sessionBlocks.ts
// 的 re-export 注释。
//
// 本文件保留的是 watchdog / 探针回答决策的纯函数（node 环境可单测，
// 组件文件不藏不可测的闭包逻辑）：
//   ② turn_state 探针回答的 stale 守卫（pinTurnStateAction）；
//   ③ believing-live 周期 turn_state 探针（pinWatchdogTick，fe0d06e/
//      a1618f7 镜像）。
// （① 流帧归约已在 P1-4 消除——见 usePinSessionLoop 头注释的三处差异。）

/** 45s 无任何入站帧 → 主动断线换新连接（重连循环走 reconcile）。 */
export const PIN_SILENCE_CLOSE_MS = 45_000;

/** watchdog 单 tick 决策（主版 useChatStreamEngine watchTimer 的纯函数化）。
 *  批次 3-4 起 close 半边由共享 useSessionSocket 的 45s 巡检持有（同常数
 *  同行为）；消费方（usePinSessionLoop）只使用 probe 半边——本函数保持
 *  双决策导出，单测继续钉住完整语义。
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
