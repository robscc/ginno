"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlertCircle,
  BookUp,
  ChevronRight,
  Loader2,
  Pencil,
  Sparkles,
  Trash2,
} from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type {
  MemoryDraft,
  MemoryDraftSummary,
  MemoryPoolPage,
  MemoryRun,
  MemoryStatus,
  MemoryThrottle,
  MemoryTrendRow,
  MemoryUsageSummary,
} from "@/lib/types";
import { MemoryDraftModal } from "@/components/right/MemoryDraftModal";
import { PromoteModal } from "@/components/right/PromoteModal";

// Settings → 智能体 → 记忆 (docs/dream-mode-design.md §4). Shows what memory is
// in effect this turn, the draft queue, the run log (incl. skips), pool samples
// and the distillation settings in one place — the 2026-10-01 stall was
// invisible precisely because none of it existed.

type KbForm = {
  capture: boolean;
  auto_summarize: boolean;
  pool_flush_threshold: number;
  memory_budget_chars: number;
  // 注入预算住在 settings.context.memory_max_chars(world_state 注入截断线),
  // 不归 knowledge 块管——保存时单独拆出来走整份 settings 读改写。
  injection_max_chars: number;
  summarize_model: string;
  // 具体模型 id;空 = 供应商默认模型(build_model 的 model_name 覆盖)
  summarize_model_id: string;
  draft_ttl_days: number;
  draft_max_pending: number;
  draft_keep: number;
  dream_nightly: boolean;
  dream_at: string;
};

const FORM_DEFAULTS: KbForm = {
  capture: true,
  auto_summarize: true,
  pool_flush_threshold: 30,
  memory_budget_chars: 3000,
  injection_max_chars: 8000,
  summarize_model: "",
  summarize_model_id: "",
  draft_ttl_days: 7,
  draft_max_pending: 3,
  draft_keep: 20,
  dream_nightly: false,
  dream_at: "03:00",
};

const nf = new Intl.NumberFormat();

function fmtWhen(ts?: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function ageDays(ts?: number | null): number {
  if (!ts) return 0;
  return Math.max(0, Math.floor((Date.now() / 1000 - ts) / 86400));
}

function Kpi({
  label,
  value,
  sub,
  alert,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  sub: React.ReactNode;
  alert?: boolean;
}) {
  return (
    <div
      className={`rounded-xl border bg-card px-4 py-3.5 ${
        alert ? "border-red/40" : "border-line"
      }`}
    >
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted">{label}</div>
      <div
        className={`mt-1.5 text-2xl font-semibold tracking-tight tabular-nums ${
          alert ? "text-red" : ""
        }`}
      >
        {value}
      </div>
      <div className="mt-1 text-[11.5px] text-faint">{sub}</div>
    </div>
  );
}

/** Sparkline with a dashed reference line (threshold / budget), drawn to scale. */
function TrendRow({
  label,
  values,
  ceiling,
  ceilingLabel,
  color,
}: {
  label: string;
  values: number[];
  ceiling: number;
  ceilingLabel: string;
  color: string;
}) {
  const n = values.length;
  const max = Math.max(ceiling || 0, ...values, 1);
  const W = 100;
  const H = 32;
  const pts = values
    .map((v, i) => `${(i / Math.max(n - 1, 1)) * W},${H - (v / max) * H}`)
    .join(" ");
  const ceilY = H - ((ceiling || 0) / max) * H;
  const last = values[n - 1] ?? 0;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="text-faint">{label}</span>
        <span className="tabular-nums text-muted">
          {nf.format(last)} · {ceilingLabel}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="mt-1 h-8 w-full"
        role="img"
        aria-label={`${label}: ${last}`}
      >
        {ceiling > 0 && (
          <line
            x1="0"
            x2={W}
            y1={ceilY}
            y2={ceilY}
            stroke="#34343f"
            strokeWidth="1"
            strokeDasharray="2 2"
            vectorEffect="non-scaling-stroke"
          />
        )}
        <polyline
          points={pts}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}

function StatusPill({ status }: { status: MemoryDraftSummary["status"] }) {
  const t = useTranslations("settings.memory");
  const cls =
    status === "pending"
      ? "bg-yellow/15 text-yellow"
      : status === "applied"
        ? "bg-green/15 text-green"
        : status === "expired"
          ? "bg-red/15 text-red"
          : "bg-white/5 text-faint";
  return (
    <span className={`pill ${cls}`}>
      {t(
        status === "pending"
          ? "statusPending"
          : status === "applied"
            ? "statusApplied"
            : status === "expired"
              ? "statusExpired"
              : "statusDiscarded",
      )}
    </span>
  );
}

export function MemorySettings() {
  const t = useTranslations("settings.memory");
  const g = useGinno();

  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [poolCount, setPoolCount] = useState(0);
  const [kbUsable, setKbUsable] = useState(false);
  const [drafts, setDrafts] = useState<MemoryDraftSummary[]>([]);
  const [runs, setRuns] = useState<MemoryRun[]>([]);
  const [usage, setUsage] = useState<MemoryUsageSummary | null>(null);
  const [pool, setPool] = useState<MemoryPoolPage | null>(null);
  const [trend, setTrend] = useState<MemoryTrendRow[]>([]);
  const [throttle, setThrottle] = useState<MemoryThrottle | null>(null);
  const [form, setForm] = useState<KbForm>(FORM_DEFAULTS);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [draftReview, setDraftReview] = useState<MemoryDraft | null>(null);
  const [promoteSection, setPromoteSection] = useState<{ text: string; lines: string[] } | null>(null);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [clearing, setClearing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [mem, draftRows, runRows, usageRow, poolPage, settings, trendRows] = await Promise.all([
        api.getMemory(),
        api.listMemoryDrafts(undefined, 60),
        api.getMemoryRuns(30),
        api.getMemoryUsage(7),
        api.getMemoryPool(1, 0),
        api.getSettings(),
        api.getMemoryTrend(30),
      ]);
      if (mem.ok) {
        setStatus(mem.status);
        setPoolCount(mem.pool_count);
        setKbUsable(mem.kb_usable);
      }
      setDrafts(draftRows.drafts || []);
      setRuns(runRows.runs || []);
      setUsage(usageRow);
      setPool(poolPage);
      setTrend(trendRows.trend || []);
      setThrottle(mem.throttle ?? null);
      const s = settings as Record<string, unknown>;
      const k = (s.knowledge || {}) as Partial<KbForm>;
      const c = (s.context || {}) as Record<string, unknown>;
      setForm({
        ...FORM_DEFAULTS,
        ...k,
        injection_max_chars:
          typeof c.memory_max_chars === "number" ? c.memory_max_chars : 8000,
      });
      setMsg("");
    } catch {
      setMsg(t("connError"));
    } finally {
      setLoaded(true);
    }
  }, [t]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pending = useMemo(() => drafts.filter((d) => d.status === "pending"), [drafts]);
  const oldest = pending[pending.length - 1];
  const oldestDays = ageDays(oldest?.created_at);
  const lastRun = runs[0];

  // The banner appears once a draft has sat past half its TTL — long before it
  // expires, while there is still something to review.
  const stuckDays = form.draft_ttl_days > 0 ? Math.floor(form.draft_ttl_days / 2) : 3;
  const showBanner = pending.length > 0 && oldestDays >= Math.max(1, stuckDays);

  const injected = status?.injected_chars ?? 0;
  const budget = status?.budget ?? 0;
  const pct = budget > 0 ? Math.min(100, Math.round((injected / budget) * 100)) : 0;

  async function distill() {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.summarizeMemory();
      if (r.ok && r.pool_entries === 0) setMsg(t("distillEmpty"));
      else if (r.ok) {
        setMsg(t("distilled"));
        await load();
        const p = await api.getMemoryDraft();
        if (p.ok && p.draft) setDraftReview(p);
      } else setMsg(r.error || t("connError"));
    } catch {
      setMsg(t("connError"));
    } finally {
      setBusy(false);
    }
  }

  async function openDraft(id: string) {
    try {
      const r = await api.getMemoryDraftById(id);
      if (r.ok && r.draft) setDraftReview(r);
    } catch {
      setMsg(t("connError"));
    }
  }

  async function discard(id: string) {
    try {
      await api.discardMemoryDraftById(id);
      setMsg(t("discarded"));
      await load();
      g.reloadMemoryBadge();
    } catch {
      setMsg(t("connError"));
    }
  }

  async function discardAndRerun(id: string) {
    await discard(id);
    await distill();
  }

  async function cleanupResolved() {
    try {
      const r = await api.deleteExpiredMemoryDrafts();
      setMsg(`${t("cleanup")} · ${r.removed?.length ?? 0}`);
      await load();
    } catch {
      setMsg(t("connError"));
    }
  }

  async function retryRun(runId: string) {
    setBusy(true);
    try {
      const r = await api.retryMemoryRun(runId);
      if (r.ok && !r.draft_exists && !r.skipped) setMsg(t("retryDone"));
      else if (r.draft_exists || r.skipped) setMsg(t("retrySkipped"));
      else if ((r.error || "").includes("model build failed")) setMsg(t("reasonModel"));
      else setMsg(r.error || t("connError"));
      await load();
    } catch {
      setMsg(t("connError"));
    } finally {
      setBusy(false);
    }
  }

  async function removeSection(title: string, text: string) {
    const lines = [title, ...text.split("\n").filter((l) => l.trim())];
    try {
      const r = await api.removeMemorySection(lines);
      if (r.ok) {
        setMsg(t("saved"));
        await load();
      }
    } catch {
      setMsg(t("connError"));
    }
  }

  async function saveEdit() {
    try {
      const r = await api.putMemory(editText);
      if (r.ok) {
        setMsg(t("saved"));
        setEditing(false);
        await load();
      }
    } catch {
      setMsg(t("connError"));
    }
  }

  async function clearPool() {
    if (!clearing) {
      setClearing(true);
      setTimeout(() => setClearing(false), 3000);
      return;
    }
    setClearing(false);
    try {
      await api.clearMemoryPool();
      setMsg(t("clearDone"));
      await load();
    } catch {
      setMsg(t("connError"));
    }
  }

  async function saveSettings() {
    setBusy(true);
    try {
      const { injection_max_chars, ...kb } = form;
      const r = await api.kbWikiPutConfig(kb);
      if (!r.ok) {
        setMsg(t("connError"));
        return;
      }
      // PUT /settings 是整文件替换:先读当前值再合并,读失败绝不写
      // (同 ExternalAgentsSettings 的防误清空守卫)。
      const cur = (await api.getSettings()) as Record<string, unknown>;
      if (!cur || typeof cur !== "object" || Object.keys(cur).length === 0) {
        setMsg(t("connError"));
        return;
      }
      const n = Math.round(Number(injection_max_chars));
      cur.context = {
        ...(cur.context || {}),
        // 0 = 不截断(world_state render 的 budget>0 判断);非法输入回落默认
        memory_max_chars: Number.isFinite(n) ? Math.max(0, Math.min(100000, n)) : 8000,
      };
      await api.putSettings(cur);
      setMsg(t("saved"));
      await load();
    } catch {
      setMsg(t("connError"));
    } finally {
      setBusy(false);
    }
  }

  const set = <K extends keyof KbForm>(key: K, value: KbForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  // 蒸馏实际生效的 provider:显式选择 > 默认 provider(模型覆盖对两者都有效)
  const distillProvider =
    form.summarize_model && g.providers[form.summarize_model]
      ? form.summarize_model
      : g.defaultProvider;
  // 供应商的可选模型:models[] 优先;老配置只有单值 default_model/model 时退化为单元素
  const provModels = (pid: string): string[] => {
    const p = g.providers[pid];
    if (!p) return [];
    if (p.models?.length) return p.models;
    return [p.default_model || p.model || ""].filter(Boolean);
  };

  if (!loaded) {
    return (
      <div className="px-8 py-10 text-center text-sm text-faint">{t("loading")}</div>
    );
  }

  const threshold = form.pool_flush_threshold || 0;
  const overX = threshold > 0 ? (poolCount / threshold).toFixed(1) : "0";
  const runStatus = (s?: string) =>
    s === "failed" ? t("runFailed") : s === "skipped" ? t("runSkipped") : t("runSuccess");
  const reasonText = (r?: string | null, ttl?: number) => {
    if (!r) return "";
    if (r === "expired_ttl") return t("reasonExpiredTtl", { ttl: ttl ?? form.draft_ttl_days });
    if (r === "evicted_capacity") return t("reasonEvicted");
    if (r === "model_build_failed") return t("reasonModel");
    if (r === "summarization_failed") return t("reasonFailed");
    if (r === "empty_draft") return t("reasonEmpty");
    return r;
  };

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">{t("title")}</h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">{t("description")}</p>
      <p className="mt-0.5 text-xs text-faint">{t("storedIn")}</p>

      {showBanner && oldest && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-red/30 bg-red/10 px-3 py-2 text-xs text-red">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <div className="min-w-0">
            <div className="font-medium">
              {t("bannerTitle", { days: oldestDays })}
            </div>
            <div className="mt-0.5 leading-relaxed opacity-90">
              {t("bannerBody", {
                when: fmtWhen(oldest.created_at),
                pool: poolCount,
              })}
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                onClick={() => openDraft(oldest.id)}
                className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
              >
                {t("bannerReview")}
              </button>
              <button
                onClick={() => discardAndRerun(oldest.id)}
                className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-txt"
              >
                {t("bannerDiscard")}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Kpi
          label={t("kpiPool")}
          value={`${nf.format(poolCount)}`}
          sub={t("kpiPoolSub", {
            threshold,
            range: pool?.first_ts ? `${fmtWhen(pool.first_ts).slice(0, 5)} → ${fmtWhen(pool.last_ts).slice(0, 5)}` : "—",
            x: overX,
          })}
        />
        <Kpi
          label={t("kpiDrafts")}
          value={pending.length}
          sub={
            oldest
              ? `${t("kpiAge", { days: oldestDays })} · ${nf.format(oldest.chars)} / ${nf.format(oldest.budget)}`
              : t("kpiNoAge")
          }
          alert={pending.length > 0}
        />
        <Kpi
          label={t("kpiLast")}
          value={lastRun ? fmtWhen(lastRun.ts).slice(0, 5) : "—"}
          sub={
            lastRun
              ? t("kpiLastSub", {
                  trigger: lastRun.trigger || "—",
                  status: runStatus(lastRun.status),
                  duration: ((lastRun.duration_ms || 0) / 1000).toFixed(1),
                  input: nf.format(lastRun.input_tokens || 0),
                  output: nf.format(lastRun.output_tokens || 0),
                })
              : t("kpiNever")
          }
        />
        <Kpi
          label={
            <>
              {t("kpiUsage", { days: 7 })}
              <span className="pill bg-white/5 text-faint">source=memory</span>
            </>
          }
          value={usage?.calls ?? 0}
          sub={t("kpiUsageSub", {
            calls: usage?.calls ?? 0,
            input: nf.format(usage?.input_tokens || 0),
            output: nf.format(usage?.output_tokens || 0),
          })}
        />
      </div>

      {/* 已生效的记忆 */}
      <section className="mt-3 rounded-xl border border-line bg-card">
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
          <h3 className="text-[13px] font-semibold">{t("effectiveTitle")}</h3>
          <span className="text-[11px] text-faint">{t("effectiveHint")}</span>
          <div className="flex-1" />
          <span className="pill bg-white/5 text-muted">
            {nf.format(injected)} / {nf.format(budget)} · {pct}%
          </span>
          <button
            onClick={() => {
              setEditText(status?.sections.map((s) => s.text).join("\n\n") || "");
              setEditing(true);
            }}
            className="flex items-center gap-1 rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-txt"
          >
            <Pencil className="h-3 w-3" />
            {t("edit")}
          </button>
        </header>
        <div className="grid lg:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
          <div className="min-w-0 px-4 py-3 lg:border-r lg:border-line">
            <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-faint">
              <span>{t("globalLabel")}</span>
              <span>
                ·{" "}
                {t("globalMeta", {
                  sections: status?.section_count ?? 0,
                  lines: status?.line_count ?? 0,
                  when: fmtWhen(status?.updated_at),
                })}
              </span>
            </div>
            {!status?.sections.length && (
              <div className="py-4 text-center text-xs text-faint">{t("empty")}</div>
            )}
            {status?.sections.map((s) => {
              const open = !!expanded[s.heading];
              return (
                <div key={s.heading} className="border-b border-white/5 last:border-b-0">
                  <div className="flex items-center gap-2 py-2">
                    <button
                      onClick={() => setExpanded((m) => ({ ...m, [s.heading]: !open }))}
                      aria-expanded={open}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left"
                    >
                      <ChevronRight
                        className={`h-3 w-3 shrink-0 text-faint transition-transform ${open ? "rotate-90" : ""}`}
                      />
                      <span className="truncate text-[12.5px] text-txt">{s.title}</span>
                      <span className="whitespace-nowrap text-[11px] text-faint tabular-nums">
                        {s.lines} · {nf.format(s.chars)}
                      </span>
                    </button>
                    {kbUsable && (
                      <button
                        onClick={() => setPromoteSection({ text: s.text, lines: [s.heading, ...s.text.split("\n")] })}
                        title={t("promote")}
                        className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 text-[11px] text-muted hover:bg-card2 hover:text-txt"
                      >
                        <BookUp className="h-3 w-3" />
                        {t("promote")}
                      </button>
                    )}
                    <button
                      onClick={() => removeSection(s.heading, s.text)}
                      title={t("removeSection")}
                      className="flex items-center gap-1 rounded-lg border border-line px-2 py-1 text-[11px] text-muted hover:bg-card2 hover:text-red"
                    >
                      <Trash2 className="h-3 w-3" />
                      {t("removeSection")}
                    </button>
                  </div>
                  {open && (
                    <pre className="whitespace-pre-wrap pb-2 pl-5 text-[11.5px] leading-relaxed text-muted">
                      {s.text}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>

          <div className="min-w-0 px-4 py-3">
            <div className="mb-1 text-[11px] text-faint">{t("injectionBudget")}</div>
            <div className="my-2 flex h-1.5 overflow-hidden rounded-full bg-card2">
              <span
                className="bg-violet"
                style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
              />
            </div>
            <div className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs">
              <span className="text-faint">{t("legendGlobal")}</span>
              <span className="tabular-nums text-txt">{nf.format(status?.chars ?? 0)}</span>
            </div>
            <div className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs">
              <span className="text-faint">
                {t("legendAgent", { agent: status?.agent_id || "—" })}
              </span>
              <span className="tabular-nums text-txt">{nf.format(status?.agent_chars ?? 0)}</span>
            </div>
            <div className="flex items-baseline justify-between py-1.5 text-xs">
              <span className="text-faint">{t("totalBudget")}</span>
              <span className="tabular-nums text-txt">
                {nf.format(injected)} / {nf.format(budget)}
              </span>
            </div>
            {status?.truncated && (
              <div className="mt-2 rounded-lg border border-yellow/30 bg-yellow/10 px-2.5 py-1.5 text-[11px] text-yellow">
                {t("budgetNote")}
              </div>
            )}
            <p className="mt-2 text-[11px] leading-relaxed text-faint">{t("budgetNote")}</p>

            <div className="mb-1 mt-3 text-[11px] text-faint">{t("agentsLabel")}</div>
            {status?.agents.map((a) => (
              <div
                key={a.id}
                className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs last:border-b-0"
              >
                <span className={a.selected ? "text-txt" : "text-muted"}>{a.id}</span>
                <span className="tabular-nums text-faint">
                  {t("agentRow", { chars: nf.format(a.chars), n: a.entries })}
                </span>
              </div>
            ))}
            <p className="mt-2 text-[11px] leading-relaxed text-faint">{t("agentsNote")}</p>
          </div>
        </div>
      </section>

      <div className="mt-3 grid min-w-0 gap-3 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="flex min-w-0 flex-col gap-3">
          {/* 草稿队列 */}
          <section className="min-w-0 rounded-xl border border-line bg-card">
            <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
              <h3 className="text-[13px] font-semibold">{t("draftsTitle")}</h3>
              <span className="text-[11px] text-faint">{t("draftsHint")}</span>
              <div className="flex-1" />
              <button
                onClick={cleanupResolved}
                className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-txt"
              >
                {t("cleanup")}
              </button>
            </header>
            {drafts.length === 0 ? (
              <div className="px-4 py-6 text-center text-xs text-faint">{t("noDrafts")}</div>
            ) : (
              drafts.slice(0, 12).map((d) => (
                <div
                  key={d.id}
                  className="flex flex-wrap items-center gap-3 border-b border-white/5 px-4 py-2.5 last:border-b-0"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[12.5px] tabular-nums text-txt">
                        {fmtWhen(d.created_at)}
                      </span>
                      <span className="pill bg-white/5 text-faint">{d.trigger}</span>
                      <StatusPill status={d.status} />
                      {d.status === "pending" && ageDays(d.created_at) > 0 && (
                        <span className="pill bg-red/15 text-red">
                          {t("ageChip", { days: ageDays(d.created_at) })}
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-3 text-[11.5px] text-faint">
                      <span>{t("draftMeta", { pool: d.pool_entries })}</span>
                      <span className={d.over_budget ? "text-red" : ""}>
                        {d.over_budget
                          ? t("draftOver", {
                              chars: nf.format(d.chars),
                              budget: nf.format(d.budget),
                              pct: Math.round(((d.chars - d.budget) / Math.max(d.budget, 1)) * 100),
                            })
                          : `${nf.format(d.chars)} / ${nf.format(d.budget)}`}
                      </span>
                      <span>{d.model || d.provider || "—"}</span>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {d.status === "pending" ? (
                      <>
                        <button
                          onClick={() => openDraft(d.id)}
                          className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-txt"
                        >
                          {t("viewDiff")}
                        </button>
                        <button
                          onClick={() => discard(d.id)}
                          className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-red"
                        >
                          {t("discard")}
                        </button>
                      </>
                    ) : (
                      <span className="text-[11px] text-faint">
                        {d.status === "expired" ? reasonText(d.expired_reason) : fmtWhen(d.resolved_at)}
                      </span>
                    )}
                  </div>
                </div>
              ))
            )}
          </section>

          {/* 运行历史 */}
          <section className="min-w-0 rounded-xl border border-line bg-card">
            <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
              <h3 className="text-[13px] font-semibold">{t("runsTitle")}</h3>
              <span className="text-[11px] text-faint">{t("runsHint")}</span>
              <div className="flex-1" />
              <span className="pill bg-white/5 text-muted">{t("runsUsageChip")}</span>
            </header>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] border-collapse">
                <thead>
                  <tr>
                    {(["time", "trigger", "status", "model", "duration", "input", "output", "pool", "chars"] as const).map(
                      (h, i) => (
                        <th
                          key={h}
                          className={`whitespace-nowrap border-b border-line px-3 py-2 text-[11px] font-semibold tracking-wide text-faint ${
                            i >= 4 ? "text-right" : "text-left"
                          }`}
                        >
                          {t(`th.${h}`)}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="selectable">
                  {runs.length === 0 && (
                    <tr>
                      <td colSpan={9} className="px-3 py-8 text-center text-xs text-faint">
                        {t("noRuns")}
                      </td>
                    </tr>
                  )}
                  {runs.map((r, i) => (
                    <tr key={`${r.ts}-${i}`} className="border-b border-white/5 last:border-b-0 hover:bg-white/5">
                      <td className="whitespace-nowrap px-3 py-2 text-[12.5px] tabular-nums text-faint">
                        {fmtWhen(r.ts)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-[12.5px] text-muted">{r.trigger || "—"}</td>
                      <td className="whitespace-nowrap px-3 py-2 text-[12.5px]">
                        <span className="inline-flex items-center gap-1.5">
                          <i
                            className={`inline-block h-1.5 w-1.5 rounded-full ${
                              r.status === "failed"
                                ? "bg-red"
                                : r.status === "skipped"
                                  ? "bg-faint"
                                  : "bg-violet"
                            }`}
                          />
                          <span className={r.status === "failed" ? "text-red" : "text-muted"}>
                            {runStatus(r.status)}
                          </span>
                          {r.reason && (
                            <span className="text-[11px] text-faint">· {reasonText(r.reason)}</span>
                          )}
                          {r.status === "failed" && (
                            <button
                              onClick={() => retryRun(r.run_id || String(r.ts))}
                              disabled={busy}
                              className="ml-1 whitespace-nowrap rounded border border-line px-1.5 py-0.5 text-[10.5px] text-muted hover:bg-card2 hover:text-txt disabled:opacity-50"
                            >
                              {t("retry")}
                            </button>
                          )}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-[12.5px] text-muted">{r.model || "—"}</td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">
                        {r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "—"}
                      </td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">
                        {r.input_tokens ? nf.format(r.input_tokens) : "—"}
                      </td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">
                        {r.output_tokens ? nf.format(r.output_tokens) : "—"}
                      </td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">
                        {r.pool_entries ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">
                        {r.chars ? `${nf.format(r.chars)}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex flex-wrap items-center gap-x-3 border-t border-line px-4 py-2.5 text-[11px] text-faint">
              <span>{t("runsFoot")}</span>
              <span className={throttle?.active ? "text-yellow" : "text-faint"}>
                {throttle?.active
                  ? t("throttle", { time: fmtWhen(throttle.until).slice(-5) })
                  : t("throttleIdle")}
              </span>
            </div>
          </section>
        </div>

        <div className="flex min-w-0 flex-col gap-3">
          {/* 近 30 天：池水位 / 记忆占用 */}
          <section className="rounded-xl border border-line bg-card">
            <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
              <h3 className="text-[13px] font-semibold">{t("trendTitle", { days: 30 })}</h3>
              <div className="flex-1" />
              <span className="text-[11px] text-faint">{t("trendHint")}</span>
            </header>
            <div className="px-4 py-3">
              {trend.length === 0 ? (
                <div className="py-3 text-center text-[11px] text-faint">{t("trendEmpty")}</div>
              ) : (
                <>
                  <TrendRow
                    label={t("trendPool")}
                    values={trend.map((r) => r.pool)}
                    ceiling={threshold}
                    ceilingLabel={t("trendThreshold", { n: threshold })}
                    color="#8b5cf6"
                  />
                  <div className="mt-3">
                    <TrendRow
                      label={t("trendMemory")}
                      values={trend.map((r) => r.memory_chars)}
                      ceiling={trend[trend.length - 1].budget || budget}
                      ceilingLabel={t("trendBudget", {
                        n: nf.format(trend[trend.length - 1].budget || budget),
                      })}
                      color="#22d3ee"
                    />
                  </div>
                </>
              )}
            </div>
          </section>

          {/* 池 */}
          <section className="rounded-xl border border-line bg-card">
            <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
              <h3 className="text-[13px] font-semibold">{t("poolTitle")}</h3>
              <div className="flex-1" />
              <span className="text-[11px] text-faint">{t("poolOnly")}</span>
            </header>
            <div className="px-4 py-3">
              <div className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs">
                <span className="text-faint">{t("poolEntries")}</span>
                <span className="tabular-nums text-txt">{nf.format(poolCount)}</span>
              </div>
              <div className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs">
                <span className="text-faint">{t("poolRange")}</span>
                <span className="tabular-nums text-txt">
                  {pool?.first_ts
                    ? `${fmtWhen(pool.first_ts).slice(0, 5)} → ${fmtWhen(pool.last_ts).slice(0, 5)}`
                    : "—"}
                </span>
              </div>
              <div className="flex items-baseline justify-between border-b border-white/5 py-1.5 text-xs">
                <span className="text-faint">{t("poolCited")}</span>
                <span className="tabular-nums text-txt">
                  {nf.format(pool?.cited ?? 0)}
                  {pool && pool.total > 0 ? ` (${Math.round((pool.cited / pool.total) * 100)}%)` : ""}
                </span>
              </div>
              <div className="flex items-baseline justify-between py-1.5 text-xs">
                <span className="text-faint">{t("poolThreshold")}</span>
                <span className="tabular-nums text-txt">{threshold}</span>
              </div>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  onClick={distill}
                  disabled={busy || poolCount === 0}
                  className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Sparkles className="h-3 w-3" />}
                  {t("distillNow")}
                </button>
                <button
                  onClick={clearPool}
                  className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-red"
                >
                  {clearing ? t("clearConfirm") : t("clearPool")}
                </button>
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-faint">{t("poolNote")}</p>
            </div>
          </section>

          {/* 设置 */}
          <section className="rounded-xl border border-line bg-card">
            <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
              <h3 className="text-[13px] font-semibold">{t("settingsTitle")}</h3>
            </header>
            <div className="px-4 py-3">
              {(
                [
                  ["capture", "capture", "captureDesc"],
                  ["auto_summarize", "auto", "autoDesc"],
                  ["dream_nightly", "nightly", "nightlyDesc"],
                ] as const
              ).map(([field, label, desc]) => (
                <label
                  key={field}
                  className="flex cursor-pointer items-start gap-2 border-b border-white/5 py-2 last:border-b-0"
                >
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={!!form[field]}
                    onChange={(e) => set(field, e.target.checked)}
                  />
                  <span className="min-w-0">
                    <span className="block text-[12.5px] text-txt">{t(label)}</span>
                    <span className="block text-[11px] text-faint">{t(desc)}</span>
                  </span>
                </label>
              ))}
              {(
                [
                  ["dream_at", "nightlyAt", "nightlyAtDesc", "time"],
                  ["pool_flush_threshold", "threshold", "thresholdDesc", "number"],
                  ["memory_budget_chars", "budget", "budgetDesc", "number"],
                  ["injection_max_chars", "injectionCap", "injectionCapDesc", "number"],
                  ["draft_ttl_days", "ttl", "ttlDesc", "number"],
                  ["draft_keep", "keep", "keepDesc", "number"],
                ] as const
              ).map(([key, label, desc, kind]) => (
                <div key={key} className="flex items-center gap-2 border-b border-white/5 py-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-[12.5px] text-txt">{t(label)}</div>
                    <div className="text-[11px] text-faint">{t(desc)}</div>
                  </div>
                  <input
                    type={kind}
                    className="field w-20 py-1 text-right text-xs"
                    value={form[key] as string | number}
                    onChange={(e) =>
                      set(
                        key,
                        (kind === "number"
                          ? Number(e.target.value) || 0
                          : e.target.value) as KbForm[typeof key],
                      )
                    }
                  />
                </div>
              ))}
              <div className="mt-2">
                <label className="field-label" htmlFor="memory-distill-provider">
                  {t("distillProvider")}
                </label>
                <select
                  id="memory-distill-provider"
                  className="field text-xs"
                  value={form.summarize_model}
                  onChange={(e) => {
                    const pid = e.target.value;
                    // 换供应商后原模型 id 大概率不在新供应商的 models 里,回落默认
                    if (
                      form.summarize_model_id &&
                      !provModels(pid).includes(form.summarize_model_id)
                    ) {
                      setForm((f) => ({
                        ...f,
                        summarize_model: pid,
                        summarize_model_id: "",
                      }));
                    } else {
                      set("summarize_model", pid);
                    }
                  }}
                >
                  <option value="">{t("modelDefault")}</option>
                  {Object.keys(g.providers).map((p) => (
                    <option key={p} value={p} disabled={!g.providers[p].enabled}>
                      {p}
                      {g.providers[p].enabled ? "" : t("modelDisabled")}
                    </option>
                  ))}
                  {form.summarize_model && !g.providers[form.summarize_model] && (
                    // 已删除/改名的历史 provider:保留原值选项,避免下拉悄悄改存量配置
                    <option value={form.summarize_model}>{form.summarize_model}</option>
                  )}
                </select>
              </div>
              <div className="mt-2">
                <label className="field-label" htmlFor="memory-distill-model">
                  {t("model")}
                </label>
                <select
                  id="memory-distill-model"
                  className="field text-xs"
                  value={form.summarize_model_id}
                  onChange={(e) => set("summarize_model_id", e.target.value)}
                >
                  <option value="">{t("modelAuto")}</option>
                  {provModels(distillProvider).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                  {form.summarize_model_id &&
                    !provModels(distillProvider).includes(form.summarize_model_id) && (
                      // 手改 settings / 供应商删了该模型:保留原值,不悄悄丢
                      <option value={form.summarize_model_id}>
                        {form.summarize_model_id}
                      </option>
                    )}
                </select>
                <p className="mt-1 text-[11px] text-faint">{t("modelDesc")}</p>
              </div>
              <div className="mt-3 flex items-center gap-2">
                <button
                  onClick={saveSettings}
                  disabled={busy}
                  className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  {busy && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("save")}
                </button>
                {msg && <span className="text-[11px] text-faint">{msg}</span>}
              </div>
              <p className="mt-2 text-[11px] leading-relaxed text-faint">{t("usageNote")}</p>
            </div>
          </section>
        </div>
      </div>

      {draftReview && (
        <MemoryDraftModal
          draft={draftReview}
          draftId={draftReview.id}
          onResolved={() => {
            load();
            g.reloadMemoryBadge();
          }}
          onClose={() => setDraftReview(null)}
        />
      )}
      {promoteSection && (
        <PromoteModal
          text={promoteSection.text}
          sectionLines={promoteSection.lines}
          onDone={(note) => {
            setMsg(note);
            setPromoteSection(null);
            load();
          }}
          onClose={() => setPromoteSection(null)}
        />
      )}
      {editing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
          onClick={() => setEditing(false)}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl border border-line2 bg-card shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center border-b border-line px-4 py-3">
              <span className="text-sm font-semibold">{t("editTitle")}</span>
              <button
                onClick={() => setEditing(false)}
                className="ml-auto rounded-lg p-1 text-muted hover:bg-card2 hover:text-txt"
                aria-label={t("cancel")}
              >
                ✕
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                spellCheck={false}
                className="h-72 w-full resize-y rounded-lg border border-line bg-base/40 p-3 font-mono text-xs leading-relaxed text-txt focus:border-violet focus:outline-none"
              />
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-line px-4 py-3">
              <button
                onClick={() => setEditing(false)}
                className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted hover:bg-card2 hover:text-txt"
              >
                {t("cancel")}
              </button>
              <button
                onClick={saveEdit}
                className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
              >
                {t("save")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
