"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Loader2, MessagesSquare, Play } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { WorkflowRun } from "@/lib/types";
import { useTriggerFeedback } from "@/lib/useTriggerFeedback";
import { StudioCanvas } from "./StudioCanvas";
import { StudioLeftRail } from "./StudioLeftRail";
import { NodeInspector } from "./NodeInspector";
import { WorkflowPane } from "./WorkflowPane";
import { RunObserver } from "./RunObserver";
import { RunRightPane } from "./RunRightPane";
import { VersionsTab } from "./VersionsTab";
import { useRunInspector } from "./useRunInspector";
import { useStudioState, type StudioTab } from "./useStudioState";

const TABS: Array<[StudioTab, string]> = [
  ["design", "设计"],
  ["run", "运行"],
  ["versions", "版本"],
];

type DagNode = Record<string, unknown> & { id: string; type: string };

/**
 * The Studio (design B): a three-pane workspace where the workflow — not the
 * conversation — is the primary object.
 *
 *   left   recipes + their runs        (配方 + Runs)
 *   centre canvas / run observer / versions
 *   right  node inspector / run context / recipe meta
 *
 * Selection lives in the URL hash (see useStudioState) so a chat deep link or
 * a reload lands back where you were.
 */
export function StudioShell() {
  const g = useGinno();
  const router = useRouter();
  const studio = useStudioState();
  const { state } = studio;

  const workflows = g.workflows;
  const wf = workflows.find((w) => w.id === state.wfId) || workflows[0] || null;

  // Default to the first recipe once the store has loaded.
  useEffect(() => {
    if (!state.wfId && workflows[0]) studio.selectWorkflow(workflows[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflows, state.wfId]);

  // Runs of the selected recipe, via the filtered endpoint. Re-fetch whenever
  // the store sees one of them change, so the rail tracks status without
  // polling the whole collection itself.
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const runSig = useMemo(
    () =>
      g.workflowRuns
        .filter((r) => r.workflow_id === wf?.id)
        .map((r) => `${r.id}:${r.status}:${r.updated}`)
        .join("|"),
    [g.workflowRuns, wf?.id],
  );
  useEffect(() => {
    if (!wf?.id) {
      setRuns([]);
      return;
    }
    let alive = true;
    setRunsLoading(true);
    api
      .listWorkflowRuns({ workflow_id: wf.id })
      .then((r) => {
        if (!alive) return;
        setRuns(r);
        setRunsLoading(false);
      })
      .catch(() => alive && setRunsLoading(false));
    return () => {
      alive = false;
    };
  }, [wf?.id, runSig]);

  const { run, events, nodeStatus, nodeStats, refresh } = useRunInspector(state.runId);

  const reloadRuns = () => {
    void g.reloadWorkflowRuns();
    void refresh();
  };

  // Entering the 运行 tab with nothing selected lands on the newest run.
  useEffect(() => {
    if (state.tab === "run" && !state.runId && runs[0]) studio.selectRun(runs[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.tab, state.runId, runs]);

  const fb = useTriggerFeedback();
  const [triggerErr, setTriggerErr] = useState<string | null>(null);
  const [asideOpen, setAsideOpen] = useState(false);

  const unfilled = useMemo(() => {
    const keys = new Set<string>();
    try {
      for (const m of JSON.stringify(wf?.dsl ?? {}).matchAll(/\{\{\s*context\.([a-zA-Z0-9_]+)\s*\}\}/g)) {
        keys.add(m[1]);
      }
    } catch {
      return [] as string[];
    }
    const initial =
      ((wf?.dsl as { context?: { initial?: Record<string, unknown> } } | undefined)?.context?.initial) || {};
    const empty = (v: unknown) => v === undefined || v === null || v === "";
    return [...keys].filter((k) => empty(state.ctxOverride[k]) && empty(initial[k]));
  }, [wf?.dsl, state.ctxOverride]);

  const trigger = async () => {
    if (!wf) return;
    fb.start();
    setTriggerErr(null);
    try {
      const r = await api.triggerWorkflowRun(
        wf.id,
        Object.keys(state.ctxOverride).length ? state.ctxOverride : undefined,
      );
      const body = r as { ok?: boolean; run?: WorkflowRun; detail?: string };
      if (body.ok && body.run) {
        fb.succeed();
        studio.selectRun(body.run.id);
        studio.setTab("run");
        reloadRuns();
      } else {
        const msg = body.detail || "触发失败";
        setTriggerErr(msg);
        fb.fail(msg);
      }
    } catch {
      const msg = "无法连接运行时";
      setTriggerErr(msg);
      fb.fail(msg);
    }
  };

  const openDevSession = async () => {
    if (!wf) return;
    await g.newSession("workflow-dev", { title: `精炼流程：${wf.name}`, workflow_id: wf.id });
    router.push("/");
  };

  const node = useMemo(() => {
    const nodes = ((wf?.dsl as { nodes?: DagNode[] } | undefined)?.nodes || []) as DagNode[];
    return nodes.find((n) => n.id === state.nodeId) || null;
  }, [wf?.dsl, state.nodeId]);

  if (!wf) {
    return (
      <div className="flex h-full items-center justify-center px-8 text-center text-sm text-faint">
        还没有配方。在聊天页用「总结成流程」从会话生成，或在 设置 → 工作流 里写一份 DSL。
      </div>
    );
  }

  const busy = fb.phase === "busy";

  // Below xl the app's own sidebar + rail leave the centre too narrow for a
  // third column, so the inspector becomes an overlay drawer there.
  const wideEnough = () =>
    typeof window === "undefined" || window.matchMedia("(min-width: 1280px)").matches;

  const asideContent = (
    <>
      {state.tab === "design" &&
        (node ? (
          <NodeInspector
            wf={wf}
            node={node}
            status={nodeStatus[node.id]}
            stat={nodeStats[node.id]}
            onSaved={() => {
              void g.reloadWorkflows();
              void g.reloadWorkflowRuns();
            }}
          />
        ) : (
          <WorkflowPane
            wf={wf}
            ctxOverride={state.ctxOverride}
            onCtxChange={studio.setCtx}
            unfilled={unfilled}
          />
        ))}
      {state.tab === "run" && <RunRightPane run={run} events={events} onChanged={reloadRuns} />}
      {state.tab === "versions" && (
        <div className="space-y-2 text-[11px] text-muted">
          <div className="text-[12.5px] font-semibold text-txt">关于版本</div>
          <p>
            每次改动（检查器编辑、开发会话 apply、回滚）都会追加一个<b>不可变</b>版本。
            run 永远钉住它启动时的 dsl_version，所以旧运行始终可复现。
          </p>
          <p className="text-faint">
            当前 v{wf.version ?? 1}。回滚不会删历史——它把旧内容写成新的下一版。
          </p>
        </div>
      )}
    </>
  );

  return (
    <div className="flex min-h-0 min-w-0 flex-1">
      <div className="w-[196px] shrink-0 xl:w-[236px]">
        <StudioLeftRail
          workflows={workflows}
          runs={runs}
          runsLoading={runsLoading}
          selWfId={wf.id}
          selRunId={state.runId}
          onSelectWorkflow={(id) => {
            studio.selectWorkflow(id);
            studio.setTab("design");
          }}
          onSelectRun={(id) => {
            studio.selectRun(id);
            studio.setTab("run");
          }}
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-line bg-panel px-3 py-2">
          <span className="truncate text-[13px] font-semibold text-txt">{wf.name}</span>
          <span className="rounded border border-line2 px-1 font-mono text-[10px] text-faint">
            v{wf.version ?? 1}
          </span>
          <div className="ml-1 flex gap-0.5">
            {TABS.map(([k, label]) => (
              <button
                key={k}
                onClick={() => studio.setTab(k)}
                className={`rounded-md px-2.5 py-1 text-[11.5px] transition-colors ${
                  state.tab === k ? "bg-card2 font-medium text-txt" : "text-muted hover:text-txt"
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="ml-auto flex items-center gap-1.5">
            <button
              onClick={() => setAsideOpen((o) => !o)}
              className="btn-press rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:text-txt xl:hidden"
            >
              检查器
            </button>
            <button
              onClick={() => void openDevSession()}
              title="打开绑定该配方的 workflow-dev 会话，用对话改 DSL（带 diff 确认）"
              className="btn-press flex items-center gap-1 rounded-md border border-line2 px-2 py-1 text-[11px] text-muted hover:text-txt"
            >
              <MessagesSquare className="h-3 w-3" />
              开发会话
            </button>
            <button
              onClick={() => void trigger()}
              disabled={busy}
              title={unfilled.length ? `${unfilled.length} 个模板变量未填：${unfilled.join(", ")}` : "运行这份配方"}
              className={`btn-press flex items-center gap-1.5 rounded-md px-3 py-1 text-[11.5px] font-medium disabled:opacity-50 ${
                unfilled.length
                  ? "border border-yellow/60 bg-yellow/10 text-yellow hover:bg-yellow/20"
                  : "bg-violet text-white hover:opacity-90"
              } ${fb.animClass}`}
            >
              {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
              {busy ? "运行中…" : "运行"}
            </button>
          </div>
        </div>

        {triggerErr && (
          <div className="border-b border-red/30 bg-red/[0.06] px-3 py-1.5 text-[11px] text-red">
            {triggerErr}
          </div>
        )}

        <div className="flex min-h-0 flex-1">
          <div className="min-w-0 flex-1 overflow-hidden p-3">
            {state.tab === "design" && (
              <StudioCanvas
                dsl={wf.dsl as never}
                status={nodeStatus}
                selected={state.nodeId}
                onSelect={(id) => {
                  studio.selectNode(id);
                  // narrow window: the drawer is the only way to see the node
                  if (id && !wideEnough()) setAsideOpen(true);
                }}
                posOverrides={state.posOverrides}
                onPosChange={studio.moveNode}
                className="h-full"
              />
            )}
            {state.tab === "run" && (
              <div className="h-full overflow-y-auto pr-1">
                <RunObserver
                  wf={wf}
                  run={run}
                  events={events}
                  nodeStats={nodeStats}
                  selNode={state.nodeId}
                  onSelectNode={studio.selectNode}
                  onSelectRun={studio.selectRun}
                  onChanged={reloadRuns}
                />
              </div>
            )}
            {state.tab === "versions" && (
              <VersionsTab wf={wf} onChanged={() => void g.reloadWorkflows()} />
            )}
          </div>

          <aside className="hidden w-[326px] shrink-0 overflow-y-auto border-l border-line bg-panel p-3 xl:block">
            {asideContent}
          </aside>

          {asideOpen && (
            <div className="fixed inset-0 z-40 xl:hidden" onClick={() => setAsideOpen(false)}>
              <div
                className="absolute right-0 top-0 h-full w-[336px] max-w-[92vw] overflow-y-auto border-l border-line bg-panel p-3 shadow-2xl"
                onClick={(e) => e.stopPropagation()}
              >
                <button
                  onClick={() => setAsideOpen(false)}
                  className="mb-2 ml-auto block rounded border border-line2 px-2 py-0.5 text-[11px] text-muted hover:text-txt"
                >
                  关闭
                </button>
                {asideContent}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}