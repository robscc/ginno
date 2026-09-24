"use client";

import { useState } from "react";
import { Check, Loader2, MessageSquare, Shield, ShieldAlert, X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { HumanInputCard } from "../HumanInputCard";

/**
 * Right pane of the 运行 view: what the run is executing with, where it is
 * waiting, and what the (validation-recovery) supervisor did to it.
 */
export function RunRightPane({
  run,
  events,
  onChanged,
}: {
  run: WorkflowRun | null;
  events: WorkflowRunEvent[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  if (!run) {
    return <div className="text-[11px] text-faint">选一次运行查看上下文与干预记录。</div>;
  }

  const ctx = Object.entries(run.context_override || {});
  // Values written into context during the run, from the event stream — the
  // closest thing to a live context view without a context endpoint.
  const writes = events.filter((e) => e.kind === "context_write");
  const supEvents = events.filter((e) => e.kind === "supervisor_intervene");
  const interrupt = run.pending_interrupt;

  const resumeManual = async () => {
    setBusy(true);
    try {
      await api.resumeWorkflowRun(run.id, {});
      onChanged();
    } catch {
      /* surfaced by the observer's own error line */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <div className="text-[12.5px] font-semibold text-txt">运行上下文</div>
        <div className="mt-1.5 space-y-0.5">
          {ctx.length === 0 && <div className="text-[11px] text-faint">本次运行未覆盖任何 context 字段</div>}
          {ctx.map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-2 border-b border-line py-1">
              <span className="text-[11px] text-muted">{k}</span>
              <span className="truncate font-mono text-[11px] text-violet">{fmtVal(v)}</span>
            </div>
          ))}
        </div>
      </div>

      {writes.length > 0 && (
        <div>
          <div className="text-[11.5px] font-semibold text-txt">context 写入（{writes.length}）</div>
          <div className="mt-1 space-y-0.5">
            {writes.slice(-8).map((w, i) => (
              <div key={i} className="flex items-baseline gap-2 font-mono text-[10.5px]">
                <span className="text-faint">{fmtTs(w.ts)}</span>
                <span className="truncate text-muted">
                  {(w.keys || []).join(", ")}
                  {w.method ? ` · ${w.method}` : ""}
                </span>
                {w.node_id && <span className="ml-auto shrink-0 text-faint">{w.node_id}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {interrupt && (
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11.5px] font-semibold text-txt">
            <MessageSquare className="h-3.5 w-3.5 text-yellow" />
            等待裁决
            {interrupt.node_id && <span className="font-mono text-[10px] text-faint">@{interrupt.node_id}</span>}
          </div>
          {interrupt.kind === "human" ? (
            <HumanInputCard
              runId={run.id}
              question={interrupt.question ?? null}
              nodeTitle={(interrupt.node_id as string) ?? undefined}
            />
          ) : (
            <div className="rounded-md border border-yellow/40 bg-yellow/[0.05] p-2.5">
              <div className="text-[11px] text-yellow">
                手动暂停{interrupt.node_id ? ` @${interrupt.node_id}` : ""}
              </div>
              <button
                onClick={() => void resumeManual()}
                disabled={busy}
                className="btn-press mt-2 flex items-center gap-1.5 rounded-md bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
              >
                {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
                继续
              </button>
            </div>
          )}
        </div>
      )}

      <div className="rounded-lg border border-line bg-base/30 p-2.5">
        <div className="flex items-center gap-1.5">
          <Shield className="h-3.5 w-3.5 text-muted" />
          <span className="text-[11.5px] font-semibold text-txt">校验恢复干预</span>
          <span className="ml-auto text-[10px] text-faint">{supEvents.length} 次</span>
        </div>
        {!supEvents.length ? (
          <div className="mt-1 text-[11px] text-faint">本次运行没有触发参数校验恢复</div>
        ) : (
          <div className="mt-1.5 space-y-1">
            {supEvents.map((e, i) => {
              const action = String(e.action ?? "?");
              const ok = action === "coerce" || action === "patch_dsl";
              return (
                <div key={i} className="rounded border border-line bg-card p-1.5 text-[10.5px]">
                  <div className="flex items-center gap-1.5">
                    {ok ? (
                      <Check className="h-3 w-3 text-green" />
                    ) : action === "abort" ? (
                      <X className="h-3 w-3 text-red" />
                    ) : (
                      <ShieldAlert className="h-3 w-3 text-orange" />
                    )}
                    <span className={ok ? "text-green" : action === "abort" ? "text-red" : "text-orange"}>
                      {action}
                    </span>
                    {e.node_id && <span className="font-mono text-faint">· {e.node_id}</span>}
                  </div>
                  {typeof e.reason === "string" && e.reason && (
                    <div className="mt-0.5 text-muted">{e.reason}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        <div className="mt-2 border-t border-line pt-1.5 text-[10px] text-faint">
          这是「参数校验失败时的自动恢复」。按检查点裁决的 Supervisor 门见阶段 2/3。
        </div>
      </div>

      <div className="space-y-0.5 text-[10.5px] text-faint">
        <div className="flex justify-between">
          <span>run id</span>
          <span className="font-mono text-muted">{run.id.slice(0, 8)}</span>
        </div>
        <div className="flex justify-between">
          <span>DSL 版本</span>
          <span className="font-mono text-muted">v{run.dsl_version ?? "?"}</span>
        </div>
        {run.retried_from && (
          <div className="flex justify-between">
            <span>重跑自</span>
            <span className="font-mono text-muted">{run.retried_from.slice(0, 8)}</span>
          </div>
        )}
        {run.session_id && (
          <div className="flex justify-between">
            <span>绑定会话</span>
            <span className="font-mono text-muted">{run.session_id.slice(0, 8)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") {
    try {
      const s = JSON.stringify(v);
      return s.length > 42 ? `${s.slice(0, 40)}…` : s;
    } catch {
      return "[object]";
    }
  }
  const s = String(v);
  return s.length > 42 ? `${s.slice(0, 40)}…` : s;
}

function fmtTs(ts?: number): string {
  if (typeof ts !== "number") return "";
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}