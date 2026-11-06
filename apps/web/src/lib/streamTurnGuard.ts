/**
 * Stream turn-adoption guard & diag throttle.
 *
 * bubble_turn_mismatch 洪水（2026-11-06 实录：16aef0a3 会话 17 分钟
 * 10,051 条 WARNING）的两半治法：
 *
 * 1. **认领泛化**：旧逻辑只救「孤儿流」（客户端中途连上 socket，拿不到
 *    turn.start，orphanStreamRef 置位）。但 sidecar 重启/resume 会在
 *    **正常连接中**生成新 turn_id——旧 bubble 不重建，此后每帧
 *    ev_turn ≠ bubble.turnId，认领分支不走 → 每帧上报、内容落错归属。
 *    现改为：live bubble 的 turnId **永远跟随服务端最新 ev_turn**（会话
 *    内 turn 串行，不存在真并发串线——mismatch 必然是漂移，认领即正解）。
 *
 * 2. **上报限频**：即便认领失效（未来未知形态），同一
 *    (session, bubbleTurn, evTurn) 漂移对只上报一次——诊断洪水不再淹没
 *    真错误。
 *
 * 纯函数 + 模块级预算表，单测在 streamTurnGuard.test.ts。
 */

export interface AdoptionInput {
  bubbleTurn: string | undefined;
  evTurn: string | undefined;
  orphan: boolean;
  adopted: boolean;
}

export type AdoptionDecision = "adopt" | "no";

/** 漂移认领判定：ev_turn 与 bubble 不一致时是否把 bubble.turnId 重铸为
 *  ev_turn。孤儿首帧行为保持；非孤儿（连接中漂移）同样认领一次。 */
export function decideTurnAdoption(input: AdoptionInput): AdoptionDecision {
  const { bubbleTurn, evTurn, orphan, adopted } = input;
  if (!evTurn || !bubbleTurn || bubbleTurn === evTurn) return "no";
  if (adopted) return "no"; // 本轮已认领过（或漂移已被吸收）
  return "adopt"; // 漂移即认领——孤儿标记不再作为唯一通道
}

export interface MismatchKey {
  sid: string;
  bubble: string;
  ev: string;
}

const reported = new Set<string>();

/** 上报预算：同一漂移对终生一次（first-only）。测试可 reset。 */
export function mismatchReportBudget(key: MismatchKey): boolean {
  const k = `${key.sid}\u0000${key.bubble}\u0000${key.ev}`;
  if (reported.has(k)) return false;
  reported.add(k);
  return true;
}

/** 测试隔离用；运行时不调用（预算表跨会话存活，重启前端自然重置）。 */
export function resetDiagState(): void {
  reported.clear();
}
