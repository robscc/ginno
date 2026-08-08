"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, Play } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowDef, WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { STATUS_LABEL } from "@/components/chat/RunBlocks";
import { useTriggerFeedback } from "@/lib/useTriggerFeedback";
import { WorkflowDag } from "./WorkflowDag";
import { WorkflowLogTimeline } from "./WorkflowLogTimeline";
import { ContextEditor } from "./ContextEditor";
import { RunErrorBox } from "./RunErrorBox";

const STEP_COLOR: Record<string, string> = {
  done: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
  paused: "#f59e0b",
  cancelled: "#71717a",
  interrupted: "#f97316",
};

function fmtRunOption(r: WorkflowRun): string {
  const d = new Date(r.started * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${STATUS_LABEL[r.status] || r.status} · ${r.id.slice(0, 8)} · ${p(d.getMonth() + 1)}-${p(
    d.getDate(),
  )} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Full workflow inspector: editable context, run trigger, live DAG (click a node
 *  to filter the log), step list, and event timeline. Shared by the /workflows
 *  page (P4) and the Settings detail panel. Polls while a run is active.
 *
 *  A run selector exposes EVERY historical run of the workflow (not just the
 *  latest), so older failed runs remain inspectable — the core of the
 *  error-localization push. */
export function WorkflowInspector({ wf, runs }: { wf: WorkflowDef; runs: WorkflowRun[] }) {
  const router = useRouter();
  const g = useGinno();
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [events, setEvents] = useState<WorkflowRunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [ctxOverride, setCtxOverride] = useState<Record<string, unknown>>({});
  const [selNode, setSelNode] = useState<string | null>(null);
  const [selRunId, setSelRunId] = useState<string | null>(null);
  const fb = useTriggerFeedback();
  const [errMsg, setErrMsg] = useState<string | null>(null);

  // All runs of THIS workflow, newest first (listWorkflowRuns ordering).
  const wfRuns = runs.filter((r) => r.workflow_id === wf.id);
  const activeId = selRunId ?? wfRuns[0]?.id ?? null;

  // Switching workflow resets the selection + any in-flight view.
  useEffect(() => {
    setSelRunId(null);
    setRun(null);
    setEvents([]);
    setSelNode(null);
    setErrMsg(null);
  }, [wf.id]);

  useEffect(() => {
    if (!activeId) {
      setEvents([]);
      return;
    }
    let alive = true;
    let t: ReturnType<typeof setInterval> | undefined;
    const load = async () => {
      try {
        const [ev, allRuns] = await Promise.all([
          api.getWorkflowRunEvents(activeId),
          api.listWorkflowRuns(),
        ]);
        if (!alive) return;
        setEvents(ev.events || []);
        const cur = allRuns.find((r) => r.id === activeId);
        if (cur) {
          setRun(cur);
          if (cur.status !== "running") {
            setBusy(false);
            if (t) clearInterval(t); // terminal → stop polling (don't tick forever)
          }
        }
      } catch {
        /* sidecar down */
      }
    };
    void load();
    t = setInterval(load, 1500);
    return () => {
      alive = false;
      if (t) clearInterval(t);
    };
  }, [activeId]);

  const trigger = async () => {
    fb.start();
    setErrMsg(null);
    setBusy(true);
    setEvents([]);
    try {
      const r = await api.triggerWorkflowRun(
        wf.id,
        Object.keys(ctxOverride).length ? ctxOverride : undefined,
      );
      // json() never throws on HTTP errors — a 4xx body is {detail}, so check
      // `ok`/`run` explicitly instead of trusting the type.
      const body = r as { ok?: boolean; run?: WorkflowRun; detail?: string };
      if (body.ok && body.run) {
        setSelRunId(body.run.id); // selector follows the freshly created run
        setRun(body.run);
        fb.succeed();
      } else {
        setBusy(false); // no run to poll → unlock the button now
        const msg = body.detail || "触发失败";
        setErrMsg(msg);
        fb.fail(msg);
      }
    } catch {
      setBusy(false); // sidecar down → don't leave the button dead
      const msg = "无法连接运行时";
      setErrMsg(msg);
      fb.fail(msg);
    }
  };

  const openDevSession = async () => {
    // Open a session bound to the workflow-dev agent; the user then asks for the
    // edit in chat. The agent proposes via workflow_propose_edit → diff card.
    const s = await g.newSession("workflow-dev");
    router.push("/");
    return s;
  };

  const nodeStatus: Record<string, string> = {};
  for (const s of run?.steps || []) nodeStatus[s.id] = s.status;

  const filteredEvents = selNode ? events.filter((e) => e.node_id === selNode) : events;

  return (
    <div className="space-y-3">
      <ContextEditor dsl={wf.dsl as never} onChange={setCtxOverride} />

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-txt">执行图</span>
        {selNode && (
          <button
            onClick={() => setSelNode(null)}
            className="btn-press rounded border border-line2 px-1.5 py-0.5 text-[10px] text-faint hover:text-muted"
          >
            清除节点过滤 [{selNode}]
          </button>
        )}
        <button
          onClick={openDevSession}
          title="打开 Workflow 开发会话，用对话修改 DSL（带 diff 确认）"
          className="btn-press rounded-md border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt"
        >
          开发会话
        </button>
        {/* Historical-run selector: the latest run is the default; any older
            (e.g. failed) run can be opened for inspection. */}
        {wfRuns.length > 0 && (
          <select
            value={selRunId ?? ""}
            onChange={(e) => setSelRunId(e.target.value || null)}
            title="选择要查看的运行记录"
            className="rounded-md border border-line2 bg-card px-1.5 py-1 text-[11px] text-muted outline-none hover:text-txt"
          >
            <option value="">最新运行（共 {wfRuns.length} 次）</option>
            {wfRuns.map((r) => (
              <option key={r.id} value={r.id}>
                {fmtRunOption(r)}
              </option>
            ))}
          </select>
        )}
        <button
          onClick={trigger}
          disabled={busy || fb.phase === "busy"}
          className={`btn-press ml-auto flex items-center gap-1.5 rounded-md bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50 ${fb.animClass}`}
        >
          {busy || fb.phase === "busy" ? (
            <>
              <Loader2 className="h-3 w-3 animate-spin" /> 运行中…
            </>
          ) : (
            <>
              <Play className="h-3 w-3" /> 运行
            </>
          )}
        </button>
      </div>
      {errMsg && <div className="rounded-md border border-red/30 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red">{errMsg}</div>}

      <WorkflowDag
        dsl={wf.dsl as never}
        status={nodeStatus}
        selected={selNode}
        onSelect={setSelNode}
      />

      {run && (
        <div className="text-[11px] text-faint">
          run <span className="font-mono text-muted">{run.id.slice(0, 8)}</span> · v
          {run.dsl_version ?? "?"} ·{" "}
          <span style={{ color: STEP_COLOR[run.status] || "rgb(var(--muted))" }}>{run.status}</span>
        </div>
      )}

      {run && <RunErrorBox run={run} />}

      {run && (
        <div className="space-y-1">
          <div className="text-xs font-medium text-txt">步骤清单</div>
          <div className="space-y-0.5">
            {run.steps.map((s) => (
              <button
                key={s.id}
                onClick={() => setSelNode((n) => (n === s.id ? null : s.id))}
                className={`flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] transition-colors hover:bg-card2/50 ${
                  selNode === s.id ? "bg-card2/60" : ""
                }`}
              >
                <span
                  className="h-1.5 w-1.5 shrink-0 rounded-full"
                  style={{ background: STEP_COLOR[s.status] || "rgb(var(--faint))" }}
                />
                <span style={{ color: STEP_COLOR[s.status] || "rgb(var(--muted))" }}>{s.status}</span>
                <span className="text-txt">{s.title || s.id}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-1">
        <div className="text-xs font-medium text-txt">
          执行日志{selNode ? ` · 节点 ${selNode}` : ""}
        </div>
        <WorkflowLogTimeline events={filteredEvents} />
      </div>

      <div className="space-y-1 rounded-lg border border-line bg-base/30 p-2.5">
        <div className="text-xs font-medium text-txt">Supervisor</div>
        {(() => {
          const sup = (wf.dsl as { supervisor?: { enabled?: boolean; mode?: string } } | undefined)
            ?.supervisor;
          const on = !!sup?.enabled;
          return (
            <div className="text-[11px] text-muted">
              状态：
              <span className={on ? "text-violet" : "text-faint"}>
                {on ? `已启用 · 模式 ${sup?.mode ?? "human"}` : "未启用"}
              </span>
              <p className="mt-1 text-faint">
                监控节点（每步后观察并决定 继续/重试/跳过/改上下文/暂停）的 auto 策略与人工决策 UX
                待深入讨论，本期仅占位，尚未接入图执行。
              </p>
            </div>
          );
        })()}
      </div>
    </div>
  );
}
