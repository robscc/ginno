// 10-subagent — 子代理生命周期（spawn/status/plan/plan.cancelled）。
// 等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { Block } from "@/components/chat/blocks";
import type { SubagentPlanEvent, SubagentSpawnEvent, SubagentStatusEvent } from "@/lib/types";
import { mid } from "../../streamCore";

export function onSubagentSpawned(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 子代理生命周期事件（契约 2：广播到父 session 与子 session 的所有
  // socket）。Store 侧 upsert 会话列表的子会话行；只有当本会话就是
  // 发起父时，transcript 里追加一张发起卡片（子 socket 收到同帧但不渲染）。
  ctx.g.notifySubagentSpawned(ev as unknown as SubagentSpawnEvent);
  // delegation 的 spawned 只为侧栏建行服务:transcript 不加发起卡——
  // 委托的实时进度/结果卡由 delegate_update + subagent.status 通道负责,
  // 叠加发起卡会双渲染。
  if (ev.parent_session_id === sid && ev.type !== "delegation") {
    ctx.storeRef.current[sid] = [
      ...(ctx.storeRef.current[sid] ?? []),
      {
        id: mid(),
        role: "system" as const,
        blocks: [
          {
            kind: "subagent_spawn",
            sessionId: ev.session_id as string,
            goal: (ev.goal as string) || "",
            constraints: (ev.constraints as string) || "",
            acceptance: (ev.acceptance as string) || "",
            title: (ev.title as string) || "",
            depth: (ev.depth as number) ?? 0,
            origin: (ev.origin as "user" | "agent") || "agent",
            spawnedAt: Math.floor(Date.now() / 1000),
          },
        ],
      },
    ];
  }
}

export function onSubagentStatus(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 状态机推进（running/waiting/done/failed/stopped）：store 的子会话行
  // 即时换挡（侧栏状态点、卡片状态共用）。终态且本会话是发起父时追加
  // 结果卡片——stopped 按契约不注入（用户/级联亲手杀的），跳过。
  ctx.g.notifySubagentStatus(ev as unknown as SubagentStatusEvent);
  const st = ev.status as string;
  if (
    ev.parent_session_id === sid &&
    (st === "done" || st === "failed")
  ) {
    const childId = ev.session_id as string;
    const store = ctx.storeRef.current[sid] ?? [];
    // 去重：同一子会话的结果卡片只出现一次（注入消息的 steer.absorbed
    // 路径可能已经先渲染过）。
    const already = store.some((m) =>
      m.blocks.some((b) => b.kind === "subagent_result" && b.sessionId === childId),
    );
    if (!already) {
      ctx.storeRef.current[sid] = [
        ...store,
        {
          id: mid(),
          role: "system" as const,
          blocks: [
            {
              kind: "subagent_result",
              sessionId: childId,
              summary: (ev.result_summary as string) || "",
              error: (ev.error as string) || undefined,
            },
          ],
        },
      ];
    }
  }
}

export function onSubagentPlan(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 拆分方案（P2 共享契约 1）：/subagent 拆分 的 LLM decompose 结果，
  // 广播到当前 session 的 socket。折成系统行里的 subagent_plan 卡片；
  // 未确认前再次到来（契约 3 的「覆盖旧 plan」）→ 就地替换还在 pending
  // 的旧卡片，已决定过的卡片保留作为记录。
  const plan = ev as unknown as SubagentPlanEvent;
  const store = ctx.storeRef.current[sid] ?? [];
  const newBlock: Block = {
    kind: "subagent_plan",
    planId: plan.plan_id,
    task: plan.task || "",
    subtasks: Array.isArray(plan.subtasks) ? plan.subtasks : [],
    status: "pending",
  };
  const pendingIdx = store.findIndex((m) =>
    m.blocks.some((b) => b.kind === "subagent_plan" && b.status === "pending"),
  );
  if (pendingIdx >= 0) {
    ctx.storeRef.current[sid] = store.map((m, i) =>
      i === pendingIdx ? { ...m, blocks: [newBlock] } : m,
    );
  } else {
    ctx.storeRef.current[sid] = [
      ...store,
      { id: mid(), role: "system" as const, blocks: [newBlock] },
    ];
  }
  ctx.syncDisplay(sid);
}

export function onSubagentPlanCancelled(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 拆分方案被取消（P3 共享契约 5）：另一个窗口发了 subagent.plan.cancel，
  // runtime 广播回执——把本窗口里该 plan 还在 pending 的卡片翻成已取消态。
  // 本地发起的取消已在 decideSubagentPlan 里乐观定格，这里幂等（只动 pending）。
  const cancelledPlanId = ev.plan_id as string | undefined;
  if (!cancelledPlanId) return;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) => ({
    ...m,
    blocks: m.blocks.map((b) =>
      b.kind === "subagent_plan" && b.planId === cancelledPlanId && b.status === "pending"
        ? { ...b, status: "cancelled" as const }
        : b,
    ),
  }));
}
