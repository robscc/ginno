"use client";

// 运行子会话视图（run view 方案）：把一次 workflow run 当成一等公民在聊天壳
// 里全屏查看。顶栏版式对齐 SubagentTopBar（streamCards.tsx），主体复用
// Studio 的 RunObserver + RunRightPane 组合（组合方式同 StudioShell 的 run
// tab）。AppShell 按 activeRunId 挂载本组件；重试/从节点重跑 fork 出的新
// run 通过 onSelectRun 原地换视角，不离开视图。

import { useEffect, useState } from "react";
import { Loader2, Zap } from "lucide-react";
import { useGinno } from "@/lib/store";
import type { WorkflowDef } from "@/lib/types";
import { useRunInspector } from "../workflow/studio/useRunInspector";
import { RunObserver } from "../workflow/studio/RunObserver";
import { RunMiniDag } from "../workflow/studio/RunMiniDag";
import { RunRightPane } from "../workflow/studio/RunRightPane";
import { RUN_STATUS_META, STATUS_COLOR, fmtElapsed } from "./RunBlocks";

export function RunSubSessionView({ runId }: { runId: string }) {
  const g = useGinno();
  const [selRunId, setSelRunId] = useState(runId);
  // AppShell 按 activeRunId 重新挂载本组件，prop 一般不会中途变化；防御性
  // 同步一下（便宜的 useEffect 守卫，未来复用方式变了也不踩坑）。
  useEffect(() => setSelRunId(runId), [runId]);
  const { run, events, nodeStats, live, refresh, loading } = useRunInspector(selRunId);
  const [selNode, setSelNode] = useState<string | null>(null);
  const wf = g.workflows.find((w) => w.id === run?.workflow_id) ?? null;

  // 顶栏 elapsed 每秒走字（仅运行中需要）。
  const [, setTick] = useState(0);
  useEffect(() => {
    if (run?.status !== "running") return;
    const t = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [run?.status]);

  // 已删除的运行：inspector 拿不到快照、store 全量列表里也没有 → 居中提示 +
  // 返回。loading 期间不闪这条（快照可能还在路上）。
  if (!loading && !run && !g.workflowRuns.some((r) => r.id === selRunId)) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 text-sm text-faint">
        <span>运行记录已删除</span>
        <button
          onClick={() => g.closeRunView()}
          className="rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
        >
          返回主对话
        </button>
      </div>
    );
  }

  // 配方被删后 RunObserver 仍可渲染（它只用 wf.name）；迷你 DAG 需要真 DSL，
  // 没有真配方就跳过。
  const wfOrStub: WorkflowDef = wf ?? {
    id: run?.workflow_id ?? "",
    name: run?.name || "已删除的流程",
    description: "",
    steps: [],
  };

  const meta =
    RUN_STATUS_META[run?.status ?? ""] ??
    { emoji: "⏳", label: run?.status || "…", color: STATUS_COLOR.pending };
  const doneCount = run?.steps.filter((s) => s.status === "done").length ?? 0;
  const now = Date.now() / 1000;
  const elapsed = run
    ? run.status === "running"
      ? now - run.started
      : run.finished && run.started
        ? run.finished - run.started
        : null
    : null;

  // 返回主对话：回「绑定本 run 的会话」。原会话被删时按钮禁用（跳过去只会
  // 落到空会话或报错）。
  const parentExists =
    !!run?.present_in_session_id &&
    g.sessions.some((s) => s.id === run.present_in_session_id);
  const back = () => {
    if (!run?.present_in_session_id) return;
    g.closeRunView();
    g.setActiveSession(run.present_in_session_id);
  };

  const onChanged = () => {
    void refresh();
    void g.reloadWorkflowRuns();
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="border-b border-line bg-panel/60 px-6 py-2 text-xs">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2">
          <Zap className="h-3.5 w-3.5 shrink-0 text-violet" />
          <span
            className="min-w-0 max-w-[40%] truncate font-medium text-txt"
            title={run?.name || wfOrStub.name}
          >
            {run?.name || wfOrStub.name || "Workflow 运行"}
          </span>
          <span
            className="flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-px"
            style={{ borderColor: meta.color + "55", color: meta.color }}
            title={`状态：${meta.label}`}
          >
            <span className="text-[10px]">{meta.emoji}</span>
            {meta.label}
          </span>
          {run && (
            <span className="shrink-0 text-muted">
              {doneCount}/{run.steps.length} 步
            </span>
          )}
          {elapsed !== null && (
            <span className="shrink-0 text-faint">⏱ {fmtElapsed(elapsed)}</span>
          )}
          <button
            onClick={back}
            disabled={!parentExists}
            title={parentExists ? "回到绑定本 run 的主对话" : "原会话已删除，无法返回"}
            className="ml-auto shrink-0 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:opacity-40"
          >
            返回主对话
          </button>
        </div>
      </div>

      {/* 主体：左 observer（步骤遥测/事件流/控制，flex-1）+ 右定宽干预面板，
          外层统一滚动（RunObserver 内容是文档流，非内部滚动）。 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex min-w-0 max-w-5xl items-start px-6 py-3">
          <div className="min-w-0 flex-1">
            {loading && !run ? (
              <div className="flex h-40 items-center justify-center text-faint">
                <Loader2 className="h-4 w-4 animate-spin" />
              </div>
            ) : (
              <RunObserver
                wf={wfOrStub}
                run={run}
                events={events}
                nodeStats={nodeStats}
                selNode={selNode}
                onSelectNode={setSelNode}
                onSelectRun={setSelRunId}
                onChanged={onChanged}
                live={live}
                miniDag={
                  wf ? (
                    <RunMiniDag
                      dsl={wf.dsl as never}
                      run={run}
                      events={events}
                      selNode={selNode}
                      onSelectNode={setSelNode}
                    />
                  ) : undefined
                }
              />
            )}
          </div>
          <aside className="w-[320px] shrink-0 border-l border-line p-3">
            <RunRightPane run={run} events={events} onChanged={onChanged} />
          </aside>
        </div>
      </div>
    </div>
  );
}
