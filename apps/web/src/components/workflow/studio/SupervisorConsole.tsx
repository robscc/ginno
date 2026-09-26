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

/** fallback_reason → 中文（auto 转人工，design B P2.5）。Decision card banner 也复用。 */
export const FALLBACK_REASON_ZH: Record<string, string> = {
  "low-confidence": "置信度不足",
  "interventions-exceeded": "干预次数超限",
  "token-budget": "token 预算超限",
  "retry-limit": "重试次数超限",
  "judge-error": "裁决器异常",
};

// 运行时缺省（DSL 未写 supervisor.confidence_min 等 5 项时的生效值）。
const CONF_MIN_DEFAULT = 0.7;

type SupBudget = {
  interventions?: number;
  max_interventions?: number;
  tokens?: number;
  token_budget?: number;
};

/** 7600 → "7.6k"、20000 → "20k"、850 → "850"（同 RunObserver 的紧凑风格）。 */
function fmtK(n: number): string {
  if (n < 1000) return String(n);
  const k = (n / 1000).toFixed(1);
  return `${k.endsWith(".0") ? k.slice(0, -2) : k}k`;
}

function fmtConf(v: unknown): string {
  return typeof v === "number" ? v.toFixed(2) : "?";
}

function BudgetBar({
  label,
  value,
  max,
  fmt,
}: {
  label: string;
  value: number;
  max: number;
  fmt: (n: number) => string;
}) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const hot = max > 0 && value / max >= 0.8;
  return (
    <div className="flex items-center gap-2">
      <span className="w-[48px] shrink-0 text-[10.5px] text-muted">{label}</span>
      <div className="h-1 flex-1 overflow-hidden rounded-full bg-line">
        <div className={`h-full rounded-full ${hot ? "bg-yellow" : "bg-violet/70"}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`shrink-0 font-mono text-[10px] tabular-nums ${hot ? "text-yellow" : "text-faint"}`}>
        {fmt(value)}/{fmt(max)}
      </span>
    </div>
  );
}

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
 * Supervisor console (design B §8.5): what this recipe's governance layer is
 * configured to do, and every decision it recorded for the inspected run.
 * P2.5 auto mode: sup_eval（打分）→ sup_decision（auto 裁决，带 confidence/budget）
 * → sup_fallback（超预算/低置信度转人工），与 human 裁决同一条时间线。
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
  const rows = events.filter(
    (e) =>
      e.kind === "sup_decision" ||
      e.kind === "supervisor_decision" || // 旧 human 事件（P2）
      e.kind === "sup_eval" ||
      e.kind === "sup_fallback" ||
      (e.kind === "interrupt" && e.nature === "supervisor"),
  );
  const hasAuto = events.some(
    (e) => e.kind === "sup_decision" || e.kind === "sup_eval" || e.kind === "sup_fallback",
  );
  // budget 是累积状态，取最近一条 auto 裁决上的即可。
  const autoDecisions = events.filter((e) => e.kind === "sup_decision" && e.mode === "auto");
  const budget = (
    autoDecisions.length ? autoDecisions[autoDecisions.length - 1].budget : null
  ) as SupBudget | null;

  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-y-auto lg:grid-cols-2">
      <div className="space-y-3">
        <div className="rounded-lg border border-line bg-card p-3">
          <div className="mb-2 flex items-center gap-1.5">
            <span className="text-[12.5px] font-semibold text-txt">配置</span>
            {cfg.enabled ? (
              <span className="rounded bg-violet/15 px-1.5 py-px text-[10px] text-violet">
                {cfg.mode === "auto" ? "auto" : "human"}
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
            每个检查点的评估与裁决落 sup_eval / sup_decision 事件，可审计、可回放。
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-line bg-card p-3">
        <div className="mb-2 flex items-center gap-1.5">
          <span className="text-[12.5px] font-semibold text-txt">裁决时间线</span>
          <span className="ml-auto font-mono text-[10px] text-faint">
            {rows.length} 条 · 本次运行
          </span>
        </div>
        {hasAuto && (
          <div className="mb-2 space-y-1 rounded border border-line bg-card2/40 p-1.5">
            {budget ? (
              <>
                <BudgetBar
                  label="干预"
                  value={budget.interventions ?? 0}
                  max={budget.max_interventions ?? 0}
                  fmt={(n) => String(n)}
                />
                <BudgetBar
                  label="tokens"
                  value={budget.tokens ?? 0}
                  max={budget.token_budget ?? 0}
                  fmt={fmtK}
                />
              </>
            ) : (
              <div className="text-[10px] text-faint">auto 已启用 — 预算随首条 auto 裁决出现</div>
            )}
          </div>
        )}
        {!rows.length ? (
          <div className="py-6 text-center text-[11px] text-faint">
            {cfg.enabled
              ? "本次运行还没有触发任何检查点裁决"
              : "该配方未启用 supervisor——启用后每个检查点的裁决都会记在这里"}
          </div>
        ) : (
          <div className="space-y-1">
            {rows.map((e, i) => {
              const kind = e.kind;
              const isDecision = kind === "sup_decision" || kind === "supervisor_decision";
              const isEval = kind === "sup_eval";
              const isFallback = kind === "sup_fallback";
              const isInterrupt = kind === "interrupt";
              const dec = (e.decision as string) || "";
              const lowConf = isEval && e.verdict === "low-confidence";
              const title = isDecision
                ? typeof e.reason === "string"
                  ? e.reason
                  : undefined
                : isFallback && typeof e.detail === "string"
                  ? e.detail
                  : undefined;
              return (
                <div
                  key={i}
                  title={title}
                  className="flex items-baseline gap-2 font-mono text-[11px]"
                >
                  <span className="w-[62px] shrink-0 text-faint">{fmtTs(e.ts)}</span>
                  {isDecision ? (
                    <>
                      <span className={`w-[96px] shrink-0 ${DECISION_COLOR[dec] || "text-muted"}`}>
                        {e.mode === "auto-pending"
                          ? "auto·直通"
                          : String(e.mode || "")}
                      </span>
                      <span className={DECISION_COLOR[dec] || "text-muted"}>{dec}</span>
                      {e.mode === "auto" && (
                        <span className="rounded bg-card2 px-1 py-px text-[9.5px] text-muted">
                          conf {fmtConf(e.confidence)}
                        </span>
                      )}
                    </>
                  ) : isEval ? (
                    <>
                      <span className="w-[96px] shrink-0 text-faint">sup_eval</span>
                      <span className={lowConf ? "text-yellow" : "text-muted"}>
                        conf {fmtConf(e.confidence)} / ≥{" "}
                        {fmtConf(e.confidence_min ?? CONF_MIN_DEFAULT)}
                        {lowConf ? " · 置信度不足" : ""}
                      </span>
                    </>
                  ) : isFallback ? (
                    <>
                      <span className="w-[96px] shrink-0 text-yellow">sup_fallback</span>
                      <span className="text-yellow">
                        转人工 · {FALLBACK_REASON_ZH[String(e.reason)] || String(e.reason || "?")}
                      </span>
                      {typeof e.detail === "string" && e.detail && (
                        <span className="truncate text-muted">{e.detail}</span>
                      )}
                    </>
                  ) : (
                    <>
                      <span className="w-[96px] shrink-0 text-yellow">human·等待</span>
                      <span className="truncate text-muted">@{e.node_id || "?"}</span>
                    </>
                  )}
                  {(isDecision || isEval || isFallback) && e.node_id && (
                    <span className="truncate text-faint">@{e.node_id}</span>
                  )}
                  {isDecision && Array.isArray(e.context_patch) && e.context_patch.length > 0 && (
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
