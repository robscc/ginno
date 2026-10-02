"use client";

// 定时任务回放视图（scheduled-tasks-design.md §3.6）：prompt 目标的影子会话
// 不进会话列表，由 /scheduled 的时间条/执行记录经 openScheduleRun 打开，在
// AppShell 主区域全屏只读回看（挂载骨架同 RunSubSessionView；工作区 hidden
// 不卸载，返回无缝）。顶栏版式对齐 SubagentTopBar；主体拉
// GET /api/sessions/{id}/history 后用 ChatStream 的既有渲染组件（blocks /
// streamCards）只读呈现——无 composer、无 WS、问题卡不可答（questionLive=false）。

import { useEffect, useRef, useState } from "react";
import { Fragment } from "react";
import { ArrowLeft, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleRun } from "@/lib/types";
import { ContextBlocks, UserBlocks, type Block } from "@/components/chat/blocks";
import { AssistantBubble } from "@/components/chat/streamCards";
import { fmtClock, fmtDayPrefix, fmtDuration, statusMeta } from "@/components/scheduled/shared";

type HistoryMessage = {
  id?: string;
  role: "user" | "assistant" | "system" | string;
  agentId?: string | null;
  blocks: Block[];
  turnId?: string;
};

export function ScheduleRunView({ run: snapshot }: { run: ScheduleRun }) {
  const g = useGinno();
  const [run, setRun] = useState<ScheduleRun>(snapshot);
  const [messages, setMessages] = useState<HistoryMessage[] | null>(null); // null = 加载中
  const [historyError, setHistoryError] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // 执行记录没有单条 GET：进行中时按 task_id 翻记录页找本 run，回写状态 pill
  // 与耗时（10s 步进；终态即停）。找不到（记录已轮转）就守着快照。
  useEffect(() => {
    if (run.status !== "running") return;
    const t = setInterval(() => {
      api
        .listScheduleRuns({ task_id: run.task_id, sort: "desc", page: 1 })
        .then((r) => {
          const fresh = (r.rows ?? []).find((x) => x.run_id === run.run_id);
          if (alive.current && fresh) setRun(fresh);
        })
        .catch(() => {
          /* ignore */
        });
    }, 10000);
    return () => clearInterval(t);
  }, [run.status, run.task_id, run.run_id]);

  // 拉影子会话全量消息（只读，一次性）。
  useEffect(() => {
    if (!run.session_id) {
      setMessages([]);
      return;
    }
    setMessages(null);
    setHistoryError(false);
    api
      .getSessionHistory(run.session_id)
      .then((h) => {
        if (!alive.current) return;
        // ok:false 或空消息 = 会话已删（§4.2：记录保留、回放给「已删除」空态）。
        if (h?.ok === false) setHistoryError(true);
        setMessages(h?.messages ?? []);
      })
      .catch(() => {
        if (!alive.current) return;
        setHistoryError(true);
        setMessages([]);
      });
  }, [run.session_id]);

  const meta = statusMeta(run.status);
  const dur =
    run.started_at && (run.finished_at || run.status === "running")
      ? fmtDuration((run.finished_at ?? Date.now() / 1000) - run.started_at)
      : null;
  const tokens =
    run.input_tokens != null || run.output_tokens != null
      ? `${(run.input_tokens ?? 0).toLocaleString()} / ${(run.output_tokens ?? 0).toLocaleString()}`
      : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 顶栏：版式对齐 SubagentTopBar（streamCards.tsx） */}
      <div className="border-b border-line bg-panel/60 px-6 py-2 text-xs">
        <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2">
          <span className="shrink-0">⏰</span>
          <span className="min-w-0 max-w-[40%] truncate font-medium text-txt" title={run.task_name}>
            {run.task_name}
          </span>
          <span
            className="flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-px"
            style={{ borderColor: meta.color + "55", color: meta.color }}
            title={`状态：${meta.label}`}
          >
            {run.status === "running" && (
              <span className="h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: meta.color }} />
            )}
            {meta.glyph} {meta.label}
          </span>
          <span className="shrink-0 text-faint">
            计划 {fmtDayPrefix(run.scheduled_at)} {fmtClock(run.scheduled_at)}
            {run.trigger === "manual" && " · 手动"}
          </span>
          <span className="shrink-0 text-faint">
            开始 {fmtClock(run.started_at)}
            {run.finished_at ? ` · 结束 ${fmtClock(run.finished_at)}` : ""}
          </span>
          {dur && <span className="shrink-0 text-faint">⏱ {dur}</span>}
          {tokens && <span className="shrink-0 text-faint" title="输入 / 输出 tokens">⇅ {tokens}</span>}
          <button
            onClick={() => g.closeScheduleRun()}
            title="返回之前的视图"
            className="ml-auto flex shrink-0 items-center gap-1 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
          >
            <ArrowLeft className="h-3 w-3" /> 返回
          </button>
        </div>
        {run.error && (
          <div className="mx-auto mt-1 max-w-3xl truncate text-[11px] text-red" title={run.error}>
            {run.error}
          </div>
        )}
      </div>

      {/* 主体：只读 transcript */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <div className="mx-auto flex max-w-3xl flex-col gap-5">
          {messages === null ? (
            <div className="flex h-40 items-center justify-center text-faint">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : historyError ? (
            <div className="py-16 text-center text-sm text-faint">
              会话已删除或无法读取——执行记录仍保留（§4.2 回放数据解耦）。
            </div>
          ) : messages.length === 0 ? (
            <div className="py-16 text-center text-sm text-faint">
              {run.status === "running" ? "影子会话正在生成，稍后重开查看…" : "没有可回放的消息。"}
            </div>
          ) : (
            messages.map((m) => {
              const key = m.id ?? `${m.role}-${m.turnId ?? ""}-${Math.random()}`;
              if (m.role === "system") {
                return (
                  <div key={key} className="flex flex-col items-center gap-2">
                    <ContextBlocks
                      blocks={m.blocks.filter(
                        (b): b is Extract<Block, { kind: "context" }> => b.kind === "context",
                      )}
                    />
                  </div>
                );
              }
              if (m.role === "user") {
                return (
                  <div key={key} className="flex flex-col items-end gap-1">
                    <div className="flex w-full items-center justify-end">
                      <div className="max-w-[78%] rounded-2xl rounded-tr-md border border-line bg-card2 px-4 py-2.5 text-sm leading-relaxed text-txt">
                        <UserBlocks blocks={m.blocks} />
                      </div>
                    </div>
                  </div>
                );
              }
              return (
                <Fragment key={key}>
                  <AssistantBubble
                    agent={g.agents.find((a) => a.id === m.agentId) ?? null}
                    agentName={m.agentId ?? undefined}
                    blocks={m.blocks}
                    turnId={m.turnId}
                    // 只读回放：问题卡固定渲染为禁用态，不提供作答通道。
                    questionLive={false}
                  />
                </Fragment>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
