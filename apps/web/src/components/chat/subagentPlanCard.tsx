"use client";

// subagentPlanCard — 拆分方案卡片（subagent P2 共享契约 1/2，subagent-design.md
// §5.1/§5.2 与附录 A.7）。消费 subagent.plan WS 事件折出的 subagent_plan 系统行
// 块：逐条列出子任务的 goal / constraints / acceptance / reason，goal 与
// constraints 可编辑；底部「确认发起 N 个 / 取消」。
//
// 确认 → 上行 subagent.plan.confirm（带编辑版 subtasks），runtime 逐个 spawn、
// 走既有 subagent.spawned 事件；取消 → subagent.plan.cancel。帧的实际发送在
// useChatStreamEngine.decideSubagentPlan（持有 per-session socket），这里只回调。
// 决定后卡片定格为回执态（块上的 status 字段），编辑器随之禁用——同一 plan_id
// 不会来第二次，store 里的状态就是唯一事实。

import { useState } from "react";
import { ListChecks, Loader2 } from "lucide-react";
import type { SubagentPlanSubtask } from "@/lib/types";
import type { Block } from "@/components/chat/blocks";

type SubagentPlanBlock = Extract<Block, { kind: "subagent_plan" }>;

export function SubagentPlanCard({
  block,
  onDecide,
}: {
  block: SubagentPlanBlock;
  onDecide: (
    planId: string,
    decision: "confirm" | "cancel",
    subtasks?: SubagentPlanSubtask[],
  ) => void;
}) {
  // 编辑草稿只在挂载时从块拷贝一次：之后块被 decide 改写的只是 status，
  // 不能让一次重渲染把用户没提交的编辑吃掉。
  const [draft, setDraft] = useState<SubagentPlanSubtask[]>(() =>
    block.subtasks.map((s) => ({ ...s })),
  );
  const [busy, setBusy] = useState<null | "confirm" | "cancel">(null);
  const done = block.status !== "pending";
  const update = (i: number, patch: Partial<SubagentPlanSubtask>) =>
    setDraft((d) => d.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const decide = (decision: "confirm" | "cancel") => {
    if (busy || done) return;
    setBusy(decision);
    // 确认带编辑版 subtasks（契约 2：用户编辑过则以编辑版为准）；取消不带。
    onDecide(block.planId, decision, decision === "confirm" ? draft : undefined);
  };
  return (
    <div className="rounded-lg border border-violet/30 bg-violet/[0.04] px-3 py-2.5 text-xs">
      <div className="flex items-center gap-1.5">
        <ListChecks className="h-3.5 w-3.5 shrink-0 text-violet" />
        <span className="min-w-0 flex-1 font-medium text-txt">
          任务拆分方案 · {block.subtasks.length} 个子任务
        </span>
        {done && (
          <span
            className={`shrink-0 rounded-md border px-1.5 py-0.5 text-[10px] ${
              block.status === "confirmed"
                ? "border-green/40 bg-green/10 text-green"
                : "border-line2 bg-card2/60 text-faint"
            }`}
          >
            {block.status === "confirmed" ? "已确认" : "已取消"}
          </span>
        )}
      </div>
      {block.task && (
        <div
          className="mt-1.5 line-clamp-2 whitespace-pre-wrap break-words leading-relaxed text-muted"
          title={block.task}
        >
          任务:{block.task}
        </div>
      )}
      <div className="mt-2 flex flex-col gap-2">
        {draft.map((s, i) => (
          <div key={i} className="rounded-md border border-line bg-card/70 px-2.5 py-2">
            <div className="flex items-center gap-1.5 text-[10px] text-faint">
              <span className="rounded bg-violet/15 px-1 py-px font-medium text-violet">
                #{i + 1}
              </span>
              子任务 {i + 1}
            </div>
            <textarea
              value={s.goal}
              disabled={done}
              onChange={(e) => update(i, { goal: e.target.value })}
              rows={2}
              placeholder="子任务目标（自足的完整描述）"
              className="mt-1 w-full resize-y rounded border border-line bg-base/40 px-1.5 py-1 text-xs leading-relaxed text-txt outline-none focus:border-violet disabled:opacity-70"
            />
            <textarea
              value={s.constraints ?? ""}
              disabled={done}
              onChange={(e) => update(i, { constraints: e.target.value })}
              rows={1}
              placeholder="约束（不可碰的边界，可留空）"
              className="mt-1 w-full resize-y rounded border border-line bg-base/40 px-1.5 py-1 text-[11px] leading-relaxed text-muted outline-none focus:border-violet disabled:opacity-70"
            />
            {(s.acceptance || s.reason) && (
              <div className="mt-1 space-y-0.5 text-[11px] leading-relaxed text-faint">
                {s.acceptance && <div>验收:{s.acceptance}</div>}
                {/* 委派理由（附录 A.7）：确认环节的主要审阅物。 */}
                {s.reason && <div title={`委派理由:${s.reason}`}>理由:{s.reason}</div>}
              </div>
            )}
          </div>
        ))}
      </div>
      {done ? (
        <div className="mt-2 border-t border-line/60 pt-1.5 text-[10px] text-faint">
          {block.status === "confirmed"
            ? `已确认 · 将依次发起 ${block.subtasks.length} 个子代理（spawn 进度见下方卡片）`
            : "已取消该拆分方案"}
        </div>
      ) : (
        <div className="mt-2 flex items-center gap-2 border-t border-line/60 pt-1.5">
          <button
            onClick={() => decide("confirm")}
            disabled={!!busy || draft.some((s) => !s.goal.trim())}
            title={draft.some((s) => !s.goal.trim()) ? "存在目标为空的子任务" : "按当前内容批量发起子代理"}
            className="btn-press flex items-center gap-1 rounded-md bg-violet px-2 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy === "confirm" && <Loader2 className="h-3 w-3 animate-spin" />}
            确认发起 {draft.length} 个
          </button>
          <button
            onClick={() => decide("cancel")}
            disabled={!!busy}
            className="rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-red/40 hover:text-red disabled:opacity-50"
          >
            取消
          </button>
          <span className="ml-auto text-[10px] text-faint">目标与约束可编辑</span>
        </div>
      )}
    </div>
  );
}
