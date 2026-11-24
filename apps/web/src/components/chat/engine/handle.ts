// engine/handle — 服务端事件 reducer 薄入口（批次 6-2 拆分）。
// 原 2217 行大 switch 已按事件域等价迁移到 handlers/ 分组模块
// （01-stream … 15-mods，连续切段、组间不交叉），handlers/index.ts 的
// EVENT_HANDLERS 注册表按原 case 顺序集中登记——事件全集的验收基准是
// handleDispatch.test.ts ①层枚举快照。
// 按事件域分发后提交显示。只有高频增量允许延迟到下一帧。
// live 气泡 helper（liveBubble.ts）与通知出口（notifyOut.ts）经 re-export
// 保持外部 import 面不变（useChatStreamEngine 与测试均从本文件取符号）。

export {
  markDelivered,
  adoptOrphanStream,
  ensureLive,
  upsertTurnMeta,
  finalizeTurnMetaLocal,
  mutateLive,
} from "./liveBubble";
export { cancelPendingReplyNotice } from "./notifyOut";

import type { EngineCtx } from "./ctx";
import { EVENT_HANDLERS } from "./handlers";

const FRAME_EVENTS = new Set(["token.delta", "thinking.delta", "reasoning.summary", "tool.args_delta"]);

export function handle(
  ctx: EngineCtx,
  sid: string,
  ev: { event: string; [k: string]: unknown },
  deferDisplay = ctx.syncDisplay,
) {
  const handler = EVENT_HANDLERS.get(ev.event);
  if (handler) handler(ctx, sid, ev);
  if (FRAME_EVENTS.has(ev.event)) deferDisplay(sid);
  else ctx.syncDisplay(sid);
}

// Only the displayed session needs a pending frame. Reducers still run on receipt.
export function createDisplayScheduler(
  currentSession: () => string | null,
  commit: (sid: string) => void,
) {
  let pending: { sid: string; frame: number } | undefined;

  function cancel() {
    if (!pending) return;
    cancelAnimationFrame(pending.frame);
    pending = undefined;
  }

  function sync(sid: string) {
    if (sid !== currentSession()) return;
    cancel();
    commit(sid);
  }

  function defer(sid: string) {
    if (sid !== currentSession()) return;
    if (document.hidden) {
      sync(sid);
      return;
    }
    if (pending?.sid === sid) return;
    cancel();
    const update = {
      sid,
      frame: requestAnimationFrame(() => {
        if (pending !== update) return;
        pending = undefined;
        if (sid === currentSession()) commit(sid);
      }),
    };
    pending = update;
  }

  function flush() {
    const sid = pending?.sid;
    cancel();
    if (sid !== undefined && sid === currentSession()) commit(sid);
  }

  return { sync, defer, flush, cancel };
}
