// handlers/types — 事件 handler 的统一签名。共享上下文（store refs / 通知出口 /
// 调度动作）全部经 EngineCtx 首参传递（ctx.ts），handler 模块不新增全局可变面。

import type { EngineCtx } from "../ctx";

/** 服务端事件帧的宽松形状（与原 handle() 的 ev 参数一致，窄化在各 handler 内）。 */
export type ServerEvent = { event: string; [k: string]: unknown };

/** 单事件处理器：等价于原 switch 里一个 case（或一组共享 case 体）。 */
export type EventHandler = (ctx: EngineCtx, sid: string, ev: ServerEvent) => void;
