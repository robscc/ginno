"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import type { WorkflowRun } from "@/lib/types";
import { relTime } from "@/lib/utils";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";

function pillClass(status: string): string {
  if (status === "running") return "bg-blue/15 text-blue";
  if (status === "paused") return "bg-orange/15 text-orange";
  if (status === "done") return "bg-green/15 text-green";
  if (status === "failed") return "bg-red/15 text-red";
  return "bg-card2 text-faint";
}

type Scope = "all" | "active" | "terminal";
/** 筛选页签：value 为 wf.runList.* 下的 key（label 随渲染取译）。 */
const SCOPES: Array<[Scope, "scopeAll" | "scopeActive" | "scopeFinished"]> = [
  ["all", "scopeAll"],
  ["active", "scopeActive"],
  ["terminal", "scopeFinished"],
];
const ACTIVE = new Set(["running", "paused"]);
const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);

/**
 * The 运行 tab's own run list (方案B 打磨): the runs that used to squat in the
 * left rail, now scoped to the selected recipe inside the tab — status pill,
 * short id, progress, version, relative time; filter chips on top.
 *
 * `strip` is the same data at <1024px, where a 224px side column would eat
 * the observer: a single horizontal line of chips.
 */
export function RunListColumn({
  runs,
  runsLoading,
  selRunId,
  onSelectRun,
  variant = "rail",
}: {
  runs: WorkflowRun[];
  runsLoading?: boolean;
  selRunId: string | null;
  onSelectRun: (id: string) => void;
  variant?: "rail" | "strip";
}) {
  const [scope, setScope] = useState<Scope>("all");
  // wf 域文案 + run 状态 key 渲染（chat.status.*），与侧栏/观察器共用 hook。
  const t = useTranslations("wf.runList");
  const tCommon = useTranslations("wf.common");
  const statusLabel = useRunStatusLabel();
  const visible =
    scope === "all" ? runs : runs.filter((r) => (scope === "active" ? ACTIVE : TERMINAL).has(r.status));

  if (variant === "strip") {
    if (!runs.length) return null;
    return (
      <div className="flex items-center gap-1.5 overflow-x-auto pb-0.5">
        {runs.map((r) => {
          const on = r.id === selRunId;
          const done = r.steps.filter((s) => s.status === "done").length;
          return (
            <button
              key={r.id}
              onClick={() => onSelectRun(r.id)}
              title={`${statusLabel(r.status)} · ${tCommon("progressSteps", { done, total: r.steps.length })} · v${r.dsl_version ?? "?"}`}
              className={`flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10.5px] transition-colors ${
                on ? "border-blue bg-card2/70 text-txt" : "border-line2 text-muted hover:bg-card/60"
              }`}
            >
              <span className={`rounded px-1 py-px text-[9px] ${pillClass(r.status)}`}>
                {statusLabel(r.status)}
              </span>
              <span className="font-mono">#{r.id.slice(0, 4)}</span>
              <span className="tabular-nums text-faint">
                {done}/{r.steps.length}
              </span>
            </button>
          );
        })}
        {runsLoading && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-faint" />}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col rounded-lg border border-line bg-panel">
      <div className="border-b border-line px-2.5 py-2">
        <div className="flex items-center gap-1.5">
          <span className="text-[11.5px] font-semibold text-txt">{t("title")}</span>
          {runsLoading ? (
            <Loader2 className="h-3 w-3 animate-spin text-faint" />
          ) : (
            <span className="rounded-full bg-card2 px-1.5 text-[10px] text-faint">{runs.length}</span>
          )}
        </div>
        <div className="mt-1.5 flex gap-2 text-[10.5px]">
          {SCOPES.map(([k, key]) => (
            <button
              key={k}
              onClick={() => setScope(k)}
              className={
                scope === k
                  ? "rounded bg-card2 px-1 py-px font-medium text-txt"
                  : "px-1 py-px text-faint transition-colors hover:text-muted"
              }
            >
              {t(key)}
            </button>
          ))}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-1">
        {visible.map((r) => {
          const on = r.id === selRunId;
          const done = r.steps.filter((s) => s.status === "done").length;
          return (
            <button
              key={r.id}
              onClick={() => onSelectRun(r.id)}
              className={`block w-full border-l-2 px-2.5 py-1.5 text-left transition-colors ${
                on ? "border-blue bg-card2/70" : "border-transparent hover:bg-card/60"
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className={`rounded px-1.5 py-px text-[9.5px] ${pillClass(r.status)}`}>
                  {statusLabel(r.status)}
                </span>
                <span className="font-mono text-[10.5px] text-muted">#{r.id.slice(0, 4)}</span>
                {/* 定时触发的 run（scheduled-tasks-design §3.6/§10 决议 7） */}
                {r.origin === "schedule" && (
                  <span className="shrink-0 rounded border border-line2 px-1 text-[9px] leading-4 text-faint" title={t("scheduledTitle")}>
                    {t("scheduledBadge")}
                  </span>
                )}
                <span className="ml-auto shrink-0 font-mono text-[10px] text-faint">
                  {r.started ? relTime(r.started) : ""}
                </span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-faint">
                <span className="tabular-nums">
                  {done}/{r.steps.length}
                </span>
                <span>· v{r.dsl_version ?? "?"}</span>
                {r.status === "paused" && r.pending_interrupt?.node_id && (
                  <span className="truncate text-orange">@{r.pending_interrupt.node_id}</span>
                )}
                {r.status === "failed" && r.error_detail?.node_id && (
                  <span className="truncate text-red">@{r.error_detail.node_id}</span>
                )}
              </div>
            </button>
          );
        })}
        {!visible.length && (
          <div className="px-2.5 py-3 text-[11px] text-faint">
            {runs.length ? t("noRunsFilter") : t("noRuns")}
          </div>
        )}
      </div>
    </div>
  );
}
