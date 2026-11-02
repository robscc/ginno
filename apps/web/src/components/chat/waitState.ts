// waitState — live 气泡「尾部等待行」的判据（纯函数，供 AssistantBubble 与
// node 测试共用；vitest 按约定只测纯逻辑模块，见 vitest.config.ts）。
//
// 背景（2026-11-03 用户反馈）：多步 turn 里「上一个块已收尾 → 下一轮首个
// delta」之间是模型调用静默窗（慢网关可达十几秒），气泡已有内容时没有任何
// 活性指示，看着像卡死。判据取数据而非计时器：满足下述条件时，图正在重新
// 调模型或即将收口——该在气泡尾部给一条带秒表的活性行。

import type { Block } from "./blocks/types";

/** 最后一个内层（非 ref）块——ref chips 渲染在卡片下方，不代表流推进。 */
export function innerTail(blocks: Block[]): Block | undefined {
  for (let i = blocks.length - 1; i >= 0; i--) {
    if (blocks[i].kind !== "ref") return blocks[i];
  }
  return undefined;
}

/**
 * streaming 的 live 气泡是否处于「等待下一轮」静默窗：
 * - 尾块是已结束的工具（!pending）——最典型：tool.end 已到、下一轮模型的
 *   首个 delta 未到；
 * - 尾块是已作答/跳过的问题（ask_user resume 后模型被重新调用）；
 * - 尾块是已注入的 steer 带（吸收后模型重新生成）。
 *
 * 排除：pending 工具（spinner 自己会转）、pending 问题卡（等的是用户不是
 * 模型）、停车中的 turn（parkedTurn：权限/提案停靠卡在等用户决定，秒表
 * 空转会误导）。
 */
export function isWaitingNextRound(
  blocks: Block[],
  streaming: boolean | undefined,
  parkedTurn: boolean | undefined,
): boolean {
  if (!streaming || parkedTurn) return false;
  const tail = innerTail(blocks);
  if (!tail) return false;
  return (
    (tail.kind === "tool" && !tail.pending) ||
    (tail.kind === "question" && tail.status !== "pending") ||
    tail.kind === "steer"
  );
}
