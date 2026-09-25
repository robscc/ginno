"use client";

import type { WorkflowDef, WorkflowRunEvent } from "@/lib/types";

function fmtTs(ts?: number): string {
  if (typeof ts !== "number") return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

const DECISION_COLOR: Record<string, string> = {
  continue: "text-green",
  skip: "text-muted",
  retry: "text-blue",
  abort: "text-red",
};

type SupervisorCfg = {
  enabled?: boolean;
  mode?: string;
  checkpoints?: { after_nodes?: string[]; every_step?: boolean; on_error?: boolean };
  retry_limit?: number;
  max_interventions?: number;
  token_budget?: number;
  confidence_min?: number;
  policy?: string;
  model?: string;
};

/**
 * Supervisor console (design B §8.5, P2 = human-first): what this recipe's
 * governance layer is configured to do, and every decision it recorded for
 * the inspected run. The auto adjudicator + budgets land in P2.5 — until then
 * an auto-mode gate passes through visibly (mode "auto-pending" below).
 */
export function SupervisorConsole({
  wf,
  events,
}: {
  wf: WorkflowDef;
  events: WorkflowRunEvent[];
}) {
  const cfg = ((wf.dsl as { supervisor?: SupervisorCfg } | undefined)?.supervisor ||
    {}) as SupervisorCfg;
  const ck = cfg.checkpoints || {};
  const after = Array.isArray(ck.after_nodes) && ck.after_nodes.length > 0;
  const decisions = events.filter(
    (e) => e.kind === "supervisor_decision" || (e.kind === "interrupt" && e.nature === "supervisor"),
  );

  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-y-auto lg:grid-cols-2">
      <div className="space-y-3">
        <div className="rounded-lg border border-line bg-card p-3">
          <div className="mb-2 flex items-center gap-1.5">
            <span className="text-[12.5px] font-semibold text-txt">配置</span>
            {cfg.enabled ? (
              <span className="rounded bg-violet/15 px-1.5 py-px text-[10px] text-violet">
                {cfg.mode === "auto" ? "auto（裁决待 P2.5，当前直通）" : "human"}
              </span>
            ) : (
              <span className="rounded bg-card2 px-1.5 py-px text-[10px] text-faint">未启用</span>
            )}
          </div>
          <div className="space-y-1 text-[11.5px]">
            <div className="flex justify-between gap-3">
              <span className="text-muted">检查点</span>
              <span className="text-right text-txt">
                {after
                  ? `指定节点后：${ck.after_nodes!.join("、")}`
                  : "每个 step 之后（every_step）"}
              </span>
            </div>
            {cfg.retry_limit != null && (
              <div className="flex justify-between">
                <span className="text-muted">每步重试上限</span>
                <span className="tabular-nums text-txt">{cfg.retry_limit}</span>
              </div>
            )}
            {cfg.max_interventions != null && (
              <div className="flex justify-between">
                <span className="text-muted">单 run 干预上限</span>
                <span className="tabular-nums text-txt">{cfg.max_interventions}</span>
              </div>
            )}
            {cfg.token_budget != null && (
              <div className="flex justify-between">
                <span className="text-muted">token 预算</span>
                <span className="tabular-nums text-txt">{cfg.token_budget}</span>
              </div>
            )}
            {cfg.confidence_min != null && (
              <div className="flex justify-between">
                <span className="text-muted">auto 回退阈值</span>
                <span className="tabular-nums text-txt">{cfg.confidence_min}</span>
              </div>
            )}
            {cfg.model && (
              <div className="flex justify-between gap-3">
                <span className="text-muted">裁决模型</span>
                <span className="truncate text-txt">{cfg.model}</span>
              </div>
            )}
            {cfg.policy && (
              <div className="mt-1 border-t border-line pt-1.5 text-muted">{cfg.policy}</div>
            )}
          </div>
          <div className="mt-2 border-t border-line pt-1.5 text-[10.5px] text-faint">
            配置三层：配方 DSL 默认 → 运行前 override（P2.5）→ 运行中即时切换（P2.5）。
            所有裁决落 supervisor_decision 事件，可审计、可回放。
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-line bg-card p-3">
        <div className="mb-2 flex items-center gap-1.5">
          <span className="text-[12.5px] font-semibold text-txt">裁决时间线</span>
          <span className="ml-auto font-mono text-[10px] text-faint">
            {decisions.length} 条 · 本次运行
          </span>
        </div>
        {!decisions.length ? (
          <div className="py-6 text-center text-[11px] text-faint">
            {cfg.enabled
              ? "本次运行还没有触发任何检查点裁决"
              : "该配方未启用 supervisor——启用后每个检查点的裁决都会记在这里"}
          </div>
        ) : (
          <div className="space-y-1">
            {decisions.map((e, i) => {
              const isDecision = e.kind === "supervisor_decision";
              const dec = (e.decision as string) || "";
              return (
                <div key={i} className="flex items-baseline gap-2 font-mono text-[11px]">
                  <span className="w-[62px] shrink-0 text-faint">{fmtTs(e.ts)}</span>
                  {isDecision ? (
                    <>
                      <span className={`w-[96px] shrink-0 ${DECISION_COLOR[dec] || "text-muted"}`}>
                        {e.mode === "auto-pending"
                          ? "auto·直通"
                          : e.mode === "human"
                            ? "human"
                            : String(e.mode || "")}
                      </span>
                      <span className={DECISION_COLOR[dec] || "text-muted"}>{dec}</span>
                    </>
                  ) : (
                    <>
                      <span className="w-[96px] shrink-0 text-yellow">human·等待</span>
                      <span className="truncate text-muted">@{e.node_id || "?"}</span>
                    </>
                  )}
                  {e.node_id && isDecision && (
                    <span className="truncate text-faint">@{e.node_id}</span>
                  )}
                  {Array.isArray(e.context_patch) && e.context_patch.length > 0 && (
                    <span className="truncate text-violet">patch: {(e.context_patch as string[]).join(",")}</span>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <div className="mt-2 border-t border-line pt-1.5 text-[10.5px] text-faint">
          跨运行的待裁决项在左栏「决策收件箱」聚合。
        </div>
      </div>
    </div>
  );
}