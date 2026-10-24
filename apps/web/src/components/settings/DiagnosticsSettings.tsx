"use client";

/** Settings → 诊断（P4-1）。只读自检报告：
 *  数据完整性 / 定时任务 / 运行时 / 接口契约。
 *  报告按需生成（`GET /api/diagnostics`），页面不轮询、不写任何状态。 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Cpu,
  Database,
  Loader2,
  Plug,
  RefreshCw,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import * as api from "@/lib/runtime";
import type { DiagnosticsIssue, DiagnosticsReport } from "@/lib/runtime";

// 后端 issue id → 本地化文案 key。未知 id（后端新增检查项）原样显示，
// 避免前端必须与后端同步发版。
const ISSUE_LABEL = {
  "data.dangling_session_refs": "issues.data.dangling_session_refs",
  "data.orphan_assets": "issues.data.orphan_assets",
  "data.stale_errors": "issues.data.stale_errors",
  "data.duplicate_titles": "issues.data.duplicate_titles",
  "data.archived_sessions": "issues.data.archived_sessions",
  "tasks.failure_rate": "issues.tasks.failure_rate",
  "runtime.stale_bundle": "issues.runtime.stale_bundle",
  "contract.missing_routes": "issues.contract.missing_routes",
} as const;
type IssueLabelKey = (typeof ISSUE_LABEL)[keyof typeof ISSUE_LABEL];

const SEVERITY_RANK: Record<string, number> = { high: 3, medium: 2, low: 1, info: 0 };
const SEVERITY_TEXT: Record<string, string> = {
  high: "text-red",
  medium: "text-yellow",
  low: "text-green",
  info: "text-muted",
};
const SEVERITY_DOT: Record<string, string> = {
  high: "bg-red",
  medium: "bg-yellow",
  low: "bg-green",
  info: "bg-muted",
};

function maxSeverity(issues: DiagnosticsIssue[]): DiagnosticsIssue["severity"] | "" {
  let worst: DiagnosticsIssue["severity"] | "" = "";
  for (const i of issues) {
    if (!worst || (SEVERITY_RANK[i.severity] ?? -1) > (SEVERITY_RANK[worst] ?? -1)) worst = i.severity;
  }
  return worst;
}

function fmtUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds || 0));
  if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  if (s >= 60) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${s}s`;
}

function StatChip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{children}</span>
  );
}

function IssueList({
  issues,
  label,
  empty,
}: {
  issues: DiagnosticsIssue[];
  label: (id: string) => string;
  empty: string;
}) {
  if (issues.length === 0) {
    return (
      <div className="flex items-center gap-1.5 text-[11px] text-green">
        <CheckCircle2 className="h-3.5 w-3.5" />
        {empty}
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      {issues.map((i) => (
        <div key={i.id} className="rounded-lg border border-line bg-base/40 px-2.5 py-2">
          <div className="flex items-center gap-1.5">
            <span
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_DOT[i.severity] ?? "bg-muted"}`}
              aria-hidden
            />
            <span className={`min-w-0 flex-1 truncate text-xs ${SEVERITY_TEXT[i.severity] ?? "text-txt"}`}>
              {label(i.id)}
            </span>
            {typeof i.count === "number" && i.count > 0 && <StatChip>{i.count}</StatChip>}
          </div>
          {i.detail && (
            <div className="mt-1 break-words text-[11px] leading-snug text-faint">{i.detail}</div>
          )}
          <div className="mt-0.5 font-mono text-[10px] text-faint/70">{i.id}</div>
        </div>
      ))}
    </div>
  );
}

function SectionCard({
  icon: Ic,
  title,
  children,
}: {
  icon: LucideIcon;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-line bg-card/30 p-4">
      <div className="mb-2 flex items-center gap-2">
        <Ic className="h-4 w-4 text-muted" />
        <h3 className="text-sm font-medium text-txt">{title}</h3>
      </div>
      {children}
    </section>
  );
}

export function DiagnosticsSettings() {
  const t = useTranslations("settings.diagnostics");
  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const label = (id: string): string => {
    const key = ISSUE_LABEL[id as keyof typeof ISSUE_LABEL] as IssueLabelKey | undefined;
    return key ? t(key) : id;
  };

  async function run() {
    setLoading(true);
    setError("");
    try {
      setReport(await api.getDiagnostics());
    } catch {
      setError(t("failedConnect"));
    } finally {
      setLoading(false);
    }
  }

  const overallSeverity = report ? maxSeverity(report.issues) : "";
  const overallColor =
    !report || report.warnings === 0 ? "text-green" : SEVERITY_TEXT[overallSeverity] ?? "text-yellow";

  return (
    <div className="px-8 py-7">
      <div className="flex items-center gap-2">
        <ShieldCheck className="h-5 w-5 text-yellow" />
        <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      </div>
      <p className="mt-1 text-xs text-faint">{t("description")}</p>

      <button
        onClick={() => void run()}
        disabled={loading}
        className="mt-4 flex items-center gap-1.5 rounded-lg bg-violet px-3.5 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
      >
        {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="h-3.5 w-3.5" />}
        {loading ? t("running") : report ? t("refresh") : t("run")}
      </button>

      {loading && !report && (
        <div className="mt-5 flex items-center gap-2 text-xs text-faint">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t("running")}
        </div>
      )}

      {error && (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-red/30 bg-red/10 px-3 py-2 text-xs text-red">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {error}
        </div>
      )}

      {report && (
        <div className="mt-5 space-y-4">
          {/* 总览：按最严重级别着色 + 生成时间 + 刷新 */}
          <div className="flex flex-wrap items-center gap-2.5">
            <span className={`text-sm font-semibold ${overallColor}`}>
              {t("warnings", { n: report.warnings })}
            </span>
            {overallSeverity && report.warnings > 0 && (
              <span className={`rounded-full bg-card2 px-2 py-0.5 text-[10px] ${SEVERITY_TEXT[overallSeverity] ?? "text-muted"}`}>
                {t(`severity.${overallSeverity}`)}
              </span>
            )}
            <span className="flex items-center gap-1 text-[11px] text-faint">
              <Clock className="h-3 w-3" />
              {t("generatedAt", { time: new Date(report.generated_at * 1000).toLocaleString() })}
            </span>
            <button
              onClick={() => void run()}
              disabled={loading}
              title={t("refresh")}
              className="rounded-md p-1 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            </button>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <SectionCard icon={Database} title={t("sections.data")}>
              <IssueList issues={report.sections.data.issues} label={label} empty={t("emptySection")} />
            </SectionCard>

            <SectionCard icon={Clock} title={t("sections.tasks")}>
              <div className="mb-2 flex flex-wrap gap-1.5">
                <StatChip>{t("taskStats.total", { n: report.sections.tasks.total })}</StatChip>
                <StatChip>{t("taskStats.ok", { n: report.sections.tasks.ok })}</StatChip>
                <StatChip>{t("taskStats.failed", { n: report.sections.tasks.failed })}</StatChip>
                <StatChip>{t("taskStats.caughtUp", { n: report.sections.tasks.caught_up })}</StatChip>
              </div>
              <IssueList issues={report.sections.tasks.issues} label={label} empty={t("emptySection")} />
              {report.sections.tasks.latest_failure && (
                <div className="mt-2 rounded-lg border border-yellow/30 bg-yellow/10 px-2.5 py-1.5 text-[11px] leading-snug text-yellow">
                  <div>
                    {t("latestFailure", {
                      task: report.sections.tasks.latest_failure.task || "—",
                      error: report.sections.tasks.latest_failure.error || "—",
                    })}
                  </div>
                  {report.sections.tasks.latest_failure.hint && (
                    <div className="mt-0.5">{t("latestFailureHint", { hint: report.sections.tasks.latest_failure.hint })}</div>
                  )}
                </div>
              )}
            </SectionCard>

            <SectionCard icon={Cpu} title={t("sections.runtime")}>
              <div className="mb-2 flex flex-wrap gap-1.5">
                <StatChip>
                  {report.sections.runtime.frozen ? t("runtimeStats.frozen") : t("runtimeStats.dev")}
                </StatChip>
                <StatChip>{t("runtimeStats.uptime", { s: fmtUptime(report.sections.runtime.uptime_s) })}</StatChip>
                <StatChip>{t("runtimeStats.sessions", { n: report.sections.runtime.loaded_sessions })}</StatChip>
                {report.sections.runtime.runtime_stale && (
                  <span className="rounded-full bg-yellow/15 px-2 py-0.5 text-[11px] text-yellow">
                    {t("runtimeStats.stale")}
                  </span>
                )}
              </div>
              <IssueList issues={report.sections.runtime.issues} label={label} empty={t("emptySection")} />
            </SectionCard>

            <SectionCard icon={Plug} title={t("sections.contracts")}>
              <IssueList issues={report.sections.contracts.issues} label={label} empty={t("emptySection")} />
              {report.sections.contracts.missing_routes.length > 0 && (
                <div className="mt-2 break-words rounded-lg border border-red/30 bg-red/10 px-2.5 py-1.5 font-mono text-[11px] text-red">
                  {t("missingRoutes", { routes: report.sections.contracts.missing_routes.join(", ") })}
                </div>
              )}
            </SectionCard>
          </div>
        </div>
      )}
    </div>
  );
}
