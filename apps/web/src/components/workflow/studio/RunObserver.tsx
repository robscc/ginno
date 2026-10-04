"use client";

import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Loader2, MessagesSquare, Pause, Play, RotateCcw, Square } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { WorkflowDef, WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { useRunStatusLabel } from "@/components/chat/RunBlocks";
import { RunErrorBox } from "../RunErrorBox";
import { WorkflowLogTimeline } from "../WorkflowLogTimeline";
import type { NodeStat } from "./useRunInspector";

const STEP_COLOR: Record<string, string> = {
  done: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
  paused: "#f59e0b",
  cancelled: "#71717a",
  interrupted: "#f97316",
  skipped: "#a1a1aa",
};

function fmtDuration(sec?: number): string {
  if (sec === undefined || !Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  return `${m}m${String(Math.round(sec % 60)).padStart(2, "0")}s`;
}

/**
 * Run observer (design B §4 屏 2): the run as a first-class view — steps with
 * telemetry, the event timeline, failure triage, and the run controls.
 *
 * Phase 1 polls (useRunInspector); phase 2 swaps the transport for the
 * run-scoped WebSocket without changing this component's inputs.
 */
export function RunObserver({
  wf,
  run,
  events,
  nodeStats,
  selNode,
  onSelectNode,
  onSelectRun,
  onChanged,
  live,
  miniDag,
}: {
  wf: WorkflowDef;
  run: WorkflowRun | null;
  events: WorkflowRunEvent[];
  nodeStats: Record<string, NodeStat>;
  selNode: string | null;
  onSelectNode: (id: string | null) => void;
  onSelectRun: (id: string) => void;
  onChanged: () => void;
  live?: boolean;
  /** The mini-DAG strip (RunMiniDag), slotted between the header and the body. */
  miniDag?: ReactNode;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const g = useGinno();
  const router = useRouter();
  // wf 域文案 + run/步骤状态 key 渲染（chat.status.*）。
  const t = useTranslations("wf.observer");
  const tCommon = useTranslations("wf.common");
  const statusLabel = useRunStatusLabel();

  // 聊天打开本次运行 (阶段5): create a chat session, present the run into it
  // (POST /present re-binds present_in_session_id so run.* events stream into
  // that chat), then jump to the conversation.
  const openInChat = async () => {
    if (!run || busy) return;
    setBusy("present");
    setErr(null);
    try {
      const s = await g.newSession("dev", { title: t("sessionTitle", { name: run.name || wf.name }) });
      if (!s?.id) {
        setErr(t("sessionCreateFailed"));
        return;
      }
      await api.presentWorkflowRun(run.id, s.id);
      router.push("/");
    } catch {
      setErr(tCommon("runtimeUnreachable"));
    } finally {
      setBusy(null);
    }
  };

  const act = async (kind: string, fn: () => Promise<unknown>) => {
    setBusy(kind);
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch {
      setErr(t("opFailed"));
    } finally {
      setBusy(null);
    }
  };

  const rerunFrom = async (nodeId: string) => {
    if (busy) return;
    setBusy(`rerun:${nodeId}`);
    setErr(null);
    try {
      const r = await api.rerunWorkflowRunFrom(run!.id, nodeId);
      const body = r as { ok?: boolean; run?: WorkflowRun; detail?: string };
      if (body.ok && body.run) {
        onSelectRun(body.run.id); // follow the fork
        onChanged();
      } else {
        setErr(body.detail || t("rerunFailed"));
      }
    } catch {
      setErr(tCommon("runtimeUnreachable"));
    } finally {
      setBusy(null);
    }
  };

  if (!run) {
    return (
      <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-line px-6 text-center text-xs text-faint">
        {wf ? t("pickRun") : t("pickWorkflow")}
      </div>
    );
  }

  const paused = run.status === "paused";
  const running = run.status === "running";
  const terminal = !running && !paused;
  const filtered = selNode ? events.filter((e) => e.node_id === selNode) : events;
  const done = run.steps.filter((s) => s.status === "done").length;
  const end = run.finished ?? run.updated;
  const duration = end && run.started ? end - run.started : undefined;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12.5px] font-semibold text-txt">{t("run")}</span>
        <span className="font-mono text-[11px] text-muted">#{run.id.slice(0, 8)}</span>
        <span className="rounded border border-line2 px-1 font-mono text-[10px] text-faint">
          v{run.dsl_version ?? "?"}
        </span>
        <span className="font-mono text-[11px] text-faint">
          {statusLabel(run.status)} · {tCommon("progressSteps", { done, total: run.steps.length })} ·{" "}
          {fmtDuration(duration)}
        </span>
        {(running || paused) && (
          <span
            className={`flex items-center gap-1 rounded-full border px-1.5 py-px text-[9.5px] ${
              live
                ? "border-green/40 text-green"
                : "border-line2 text-faint"
            }`}
            title={live ? t("liveTitle") : t("pollTitle")}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${live ? "bg-green" : "bg-faint"}`} />
            {live ? t("push") : t("poll")}
          </span>
        )}

        <div className="ml-auto flex items-center gap-1.5">
          <button
            onClick={() => void openInChat()}
            disabled={busy === "present"}
            title={t("openInChatTitle")}
            className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:text-txt disabled:opacity-50"
          >
            {busy === "present" ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <MessagesSquare className="h-3 w-3" />
            )}
            {t("openInChat")}
          </button>
          {running && (
            <button
              onClick={() => void act("pause", () => api.pauseWorkflowRun(run.id))}
              disabled={!!busy}
              className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:text-txt disabled:opacity-50"
            >
              {busy === "pause" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Pause className="h-3 w-3" />}
              {t("pause")}
            </button>
          )}
          {paused && run.pending_interrupt?.kind === "manual" && (
            <button
              onClick={() => void act("resume", () => api.resumeWorkflowRun(run.id, {}))}
              disabled={!!busy}
              className="btn-press flex items-center gap-1 rounded-md bg-violet px-2.5 py-1 text-[11px] font-medium text-white hover:opacity-90 disabled:opacity-50"
            >
              {busy === "resume" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
              {t("resume")}
            </button>
          )}
          {terminal && (
            <button
              onClick={() =>
                void act("retry", async () => {
                  const r = await api.retryWorkflowRun(run.id);
                  if (r?.ok && r.run) onSelectRun(r.run.id);
                })
              }
              disabled={!!busy}
              title={t("rerunTitle")}
              className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:text-txt disabled:opacity-50"
            >
              {busy === "retry" ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
              {t("rerun")}
            </button>
          )}
          {(running || paused) && (
            <button
              onClick={() => void act("cancel", () => api.cancelWorkflowRun(run.id))}
              disabled={!!busy}
              className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:border-red/40 hover:text-red disabled:opacity-50"
            >
              {busy === "cancel" ? <Loader2 className="h-3 w-3 animate-spin" /> : <Square className="h-3 w-3" />}
              {t("abort")}
            </button>
          )}
        </div>
      </div>

      {err && (
        <div className="rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red">{err}</div>
      )}

      {miniDag}

      <RunErrorBox
        run={run}
        onRetryFromCheckpoint={async () => {
          // The checkpoint retry produces a NEW run — follow it so the observer
          // swaps onto the retry instead of lingering on the dead one.
          const r = await api.retryWorkflowRunFromCheckpoint(run.id);
          if (r?.ok && r.run) onSelectRun(r.run.id);
          else onChanged();
          return r;
        }}
      />

      {/* 方案B 打磨: steps left / event stream right — below 900px they stack. */}
      <div className="flex flex-col gap-3 min-[900px]:flex-row min-[900px]:items-start">
        <div className="w-full shrink-0 min-[900px]:w-64">
          <div className="mb-1 flex items-center gap-1.5">
            <span className="text-[11.5px] font-semibold text-txt">{t("steps")}</span>
            {selNode && (
              <button
                onClick={() => onSelectNode(null)}
                className="rounded border border-line2 px-1.5 py-px text-[10px] text-faint hover:text-muted"
              >
                {t("clearFilter", { node: selNode })}
              </button>
            )}
          </div>
          <div className="space-y-0.5">
          {run.steps.map((s) => {
            const st = nodeStats[s.id] || {};
            const on = selNode === s.id;
            return (
              <button
                key={s.id}
                onClick={() => onSelectNode(on ? null : s.id)}
                className={`flex w-full items-center gap-2 rounded px-1.5 py-0.5 text-left text-[11px] transition-colors ${
                  on ? "bg-card2/70" : "hover:bg-card/60"
                }`}
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: STEP_COLOR[s.status] || "rgb(var(--faint))" }}
                />
                <span className="w-[68px] shrink-0" style={{ color: STEP_COLOR[s.status] || "rgb(var(--muted))" }}>
                  {statusLabel(s.status)}
                </span>
                <span className="min-w-0 flex-1 truncate text-txt">{s.title || s.id}</span>
                {st.latencyMs !== undefined && (
                  <span className="shrink-0 tabular-nums text-faint">
                    {st.latencyMs >= 1000 ? `${(st.latencyMs / 1000).toFixed(1)}s` : `${Math.round(st.latencyMs)}ms`}
                  </span>
                )}
                {!!st.tokens && (
                  <span className="shrink-0 tabular-nums text-faint" title={tCommon("tokensTitle")}>
                    {st.tokens >= 1000 ? `${(st.tokens / 1000).toFixed(1)}K` : st.tokens}↑
                  </span>
                )}
                {terminal && (
                  // span 而非 button：外层步骤行已是 button，嵌套 button 会触发
                  // React hydration 警告（HTML 不允许 button 嵌套）。
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation();
                      void rerunFrom(s.id);
                    }}
                    aria-disabled={busy !== null}
                    title={t("rerunStepTitle")}
                    className="btn-press shrink-0 cursor-pointer rounded border border-line2 px-1 py-px text-[9.5px] text-faint hover:border-violet/50 hover:text-violet aria-disabled:opacity-40"
                  >
                    {busy === `rerun:${s.id}` ? "…" : t("rerunStep")}
                  </span>
                )}
              </button>
            );
          })}
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="mb-1 text-[11.5px] font-semibold text-txt">
            {t("eventStream")}
            {selNode ? tCommon("nodeSuffix", { node: selNode }) : ""}
          </div>
          <WorkflowLogTimeline events={filtered} filters />
        </div>
      </div>
    </div>
  );
}