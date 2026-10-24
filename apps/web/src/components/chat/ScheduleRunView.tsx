"use client";

// 定时任务回放视图（scheduled-tasks-design.md §3.6）：prompt 目标的影子会话
// 不进会话列表，由 /scheduled 的时间条/执行记录经 openScheduleRun 打开，在
// AppShell 主区域全屏只读回看（挂载骨架同 RunSubSessionView；工作区 hidden
// 不卸载，返回无缝）。顶栏版式对齐 SubagentTopBar；主体拉
// GET /api/sessions/{id}/history 后用 ChatStream 的既有渲染组件（blocks /
// streamCards）只读呈现——无 composer、无 WS、问题卡不可答（questionLive=false）。

import { useEffect, useRef, useState } from "react";
import { Fragment } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { ArrowLeft, Loader2, MessageSquarePlus } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ScheduleRun, SessionMeta } from "@/lib/types";
import { ContextBlocks, UserBlocks, type Block } from "@/components/chat/blocks";
import { AssistantBubble } from "@/components/chat/streamCards";
import { CHAT_COL } from "@/components/chat/columnWidth";
import { SubagentOpenOverride } from "@/components/chat/blocks/subagentCards";
import {
  SUBAGENT_STATUS_META,
  subagentStatusMeta,
  useStatusLabel,
} from "@/components/chat/blocks/types";
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
  const tr = useTranslations("sched");
  const router = useRouter();
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

  // ---- 子代理下钻（查看方式与影子会话同一套：只读回放，不进活跃会话）------
  // 入口是 transcript 里的 subagent 发起/结果卡片（SubagentOpenOverride 改道
  // 到这里）；栈结构支持嵌套 subagent 逐层进入，返回逐层弹出。
  const [subStack, setSubStack] = useState<SessionMeta[]>([]);
  useEffect(() => setSubStack([]), [snapshot.run_id]);
  const focusSub = subStack[subStack.length - 1] ?? null;
  const openSubReplay = (sid: string) => {
    setSubStack((st) =>
      st[st.length - 1]?.id === sid ? st : [...st, g.sessions.find((s) => s.id === sid) ?? ({ id: sid } as SessionMeta)],
    );
  };
  const popSubReplay = () => setSubStack((st) => st.slice(0, -1));

  // 拉当前视图会话（影子会话或下钻的子会话）的全量消息（只读）。运行中每 5s
  // 重拉——消息随执行流式落地，终态即停。
  const viewSessionId = focusSub?.id ?? run.session_id;
  useEffect(() => {
    if (!viewSessionId) {
      setMessages([]);
      return;
    }
    let stopped = false;
    const fetchOnce = () =>
      api
        .getSessionHistory(viewSessionId)
        .then((h) => {
          if (!alive.current || stopped) return;
          // ok:false 或空消息 = 会话已删（§4.2：记录保留、回放给「已删除」空态）。
          if (h?.ok === false) setHistoryError(true);
          setMessages(h?.messages ?? []);
        })
        .catch(() => {
          if (!alive.current || stopped) return;
          setHistoryError(true);
          setMessages([]);
        });
    setMessages(null);
    setHistoryError(false);
    void fetchOnce();
    let timer: number | undefined;
    if (run.status === "running") {
      timer = window.setInterval(fetchOnce, 5000);
    }
    return () => {
      stopped = true;
      if (timer !== undefined) clearInterval(timer);
    };
  }, [viewSessionId, run.status]);

  // 子会话状态点：subagent 走 subagent.status；delegation 无该字段，按
  // stop_reason 折算（running 期字面量即 "running"，与结果卡同一判定）。
  const statusLabel = useStatusLabel();
  const childStatusMeta = (s: SessionMeta) => {
    if (s.type === "delegation") {
      const stop = s.stop_reason ?? "";
      if (!stop || stop === "running") return SUBAGENT_STATUS_META.running;
      return stop === "error" || stop === "timeout"
        ? SUBAGENT_STATUS_META.failed
        : SUBAGENT_STATUS_META.done;
    }
    return subagentStatusMeta(s.subagent?.status);
  };
  // 下钻视图实时读 store（subagent.status 换挡无需重挂）。
  const liveFocus = focusSub ? g.sessions.find((s) => s.id === focusSub.id) ?? focusSub : null;

  const meta = statusMeta(run.status);
  const dur =
    run.started_at && (run.finished_at || run.status === "running")
      ? fmtDuration((run.finished_at ?? Date.now() / 1000) - run.started_at)
      : null;
  const tokens =
    run.input_tokens != null || run.output_tokens != null
      ? `${(run.input_tokens ?? 0).toLocaleString()} / ${(run.output_tokens ?? 0).toLocaleString()}`
      : null;

  // ---- 继续沟通（schedule-followup）------------------------------------------
  // fork = 复制影子会话全量历史（id 重铸）到新普通会话；影子会话保持只读
  // 快照，后续定时轮次不受影响。一 run 一副本（2026-10-14 定稿）：已 fork
  // 且副本存活 → 按钮变「跳到对应对话」，不再重复 fork（后端同守卫幂等）；
  // 副本被删 → 回到可 fork 态重新创建。
  const [forkBusy, setForkBusy] = useState(false);
  const [forkError, setForkError] = useState<string | null>(null);
  const finished = run.status === "ok" || run.status === "error";
  const canFork = finished && !!run.session_id && !historyError;
  const forkedLive =
    !!run.forked_session_id && g.sessions.some((s) => s.id === run.forked_session_id);
  const enterSession = (sid: string) => {
    // setActiveSession 自带清掉回放覆盖层；但本视图挂在 /scheduled 路由上，
    // 只切会话不清路由的话人还停在定时任务页——回工作区路由才是「进入会话」
    // （与侧栏 openChild / 发起卡 openChild 同一模式）。
    g.setActiveSession(sid);
    if (window.location.pathname !== "/") router.push("/");
  };
  const doFork = async () => {
    if (forkBusy || !run.session_id) return;
    setForkBusy(true);
    setForkError(null);
    try {
      const r = await api.forkScheduleRun(run.run_id);
      if (r.ok && r.session_id) {
        await g.reloadSessions();
        enterSession(r.session_id);
      } else {
        setForkError(r.error || tr("view.forkFailed"));
      }
    } catch {
      setForkError(tr("view.forkFailed"));
    } finally {
      setForkBusy(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 顶栏：版式对齐 SubagentTopBar（streamCards.tsx）。下钻子会话时换
          子代理版式（🤖 + goal + 状态 + 返回执行记录），回放仍是同一套只读
          transcript。 */}
      <div className="border-b border-line bg-panel/60 px-6 py-2 text-xs">
        {liveFocus ? (
          <div className={`${CHAT_COL} flex flex-wrap items-center gap-2`}>
            <span className="shrink-0">🤖</span>
            <span
              className="min-w-0 max-w-[40%] truncate font-medium text-txt"
              title={liveFocus.subagent?.goal || liveFocus.title || undefined}
            >
              {liveFocus.subagent?.goal || liveFocus.title || tr("view.subUntitled")}
            </span>
            {(() => {
              const m = childStatusMeta(liveFocus);
              const active =
                liveFocus.subagent?.status === "running" ||
                liveFocus.subagent?.status === "waiting" ||
                (liveFocus.type === "delegation" &&
                  (!liveFocus.stop_reason || liveFocus.stop_reason === "running"));
              const label =
                liveFocus.type === "delegation"
                  ? statusLabel(
                      !liveFocus.stop_reason || liveFocus.stop_reason === "running"
                        ? "running"
                        : liveFocus.stop_reason === "error" || liveFocus.stop_reason === "timeout"
                          ? "failed"
                          : "done",
                    )
                  : statusLabel(liveFocus.subagent?.status);
              return (
                <span
                  className="flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-px"
                  style={{ borderColor: m.color + "55", color: m.color }}
                >
                  {active && (
                    <span
                      className="h-1.5 w-1.5 animate-pulse rounded-full"
                      style={{ background: m.color }}
                    />
                  )}
                  {m.glyph} {label}
                </span>
              );
            })()}
            <button
              onClick={popSubReplay}
              title={tr("view.backToRunTitle")}
              className="ml-auto flex shrink-0 items-center gap-1 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
            >
              <ArrowLeft className="h-3 w-3" /> {subStack.length > 1 ? tr("view.back") : tr("view.backToRun")}
            </button>
          </div>
        ) : (
        <div className={`${CHAT_COL} flex flex-wrap items-center gap-2`}>

          <span className="shrink-0">⏰</span>
          <span className="min-w-0 max-w-[40%] truncate font-medium text-txt" title={run.task_name}>
            {run.task_name}
          </span>
          <span
            className="flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-px"
            style={{ borderColor: meta.color, color: meta.color }}
            title={tr("view.statusTitle", { label: meta.label })}
          >
            {run.status === "running" && (
              <span className="h-1.5 w-1.5 animate-pulse rounded-full" style={{ background: meta.color }} />
            )}
            {meta.glyph} {meta.label}
          </span>
          <span className="shrink-0 text-faint">
            {tr("view.due", { day: fmtDayPrefix(run.scheduled_at), clock: fmtClock(run.scheduled_at) })}
            {run.trigger === "manual" && tr("view.manualSuffix")}
            {run.trigger === "catch_up" && tr("trigger.catchUp") ? ` · ${tr("trigger.catchUp")}` : ""}
          </span>
          <span className="shrink-0 text-faint">
            {tr("view.start", { clock: fmtClock(run.started_at) })}
            {run.finished_at ? tr("view.endSuffix", { clock: fmtClock(run.finished_at) }) : ""}
          </span>
          {dur && <span className="shrink-0 text-faint">⏱ {dur}</span>}
          {tokens && <span className="shrink-0 text-faint" title={tr("view.tokensTitle")}>⇅ {tokens}</span>}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            {forkedLive ? (
              // 一 run 一副本：已 fork → 唯一动作是跳进既有会话
              <button
                onClick={() => enterSession(run.forked_session_id!)}
                title={tr("view.openForkedTitle")}
                className="flex shrink-0 items-center gap-1 rounded-md border border-violet/40 bg-violet/10 px-2 py-0.5 text-[11px] text-violet transition-colors hover:bg-violet/20"
              >
                <MessageSquarePlus className="h-3 w-3" />
                {tr("view.openForked")}
              </button>
            ) : canFork && (
              <button
                onClick={() => void doFork()}
                disabled={forkBusy}
                title={tr("view.forkTitle")}
                className="flex shrink-0 items-center gap-1 rounded-md border border-violet/40 bg-violet/10 px-2 py-0.5 text-[11px] text-violet transition-colors hover:bg-violet/20 disabled:opacity-50"
              >
                {forkBusy ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <MessageSquarePlus className="h-3 w-3" />
                )}
                {forkBusy ? tr("view.forkBusy") : tr("view.fork")}
              </button>
            )}
            <button
              onClick={() => g.closeScheduleRun()}
              title={tr("view.backTitle")}
              className="flex shrink-0 items-center gap-1 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet"
            >
              <ArrowLeft className="h-3 w-3" /> {tr("view.back")}
            </button>
          </div>
        </div>
        )}
        {!liveFocus && run.error && (
          <div className={`${CHAT_COL} mt-1 truncate text-[11px] text-red`} title={run.error}>
            {run.error}
          </div>
        )}
        {!liveFocus && forkError && (
          <div className={`${CHAT_COL} mt-1 truncate text-[11px] text-red`} title={forkError}>
            {forkError}
          </div>
        )}
      </div>

      {/* 主体：只读 transcript。卡片「查看对话/查看完整对话」经
          SubagentOpenOverride 改道到本视图内部的只读下钻——影子子树不进
          活跃会话。 */}
      <div className="flex-1 overflow-y-auto px-6 py-6">
        <SubagentOpenOverride.Provider value={{ open: openSubReplay }}>
        <div className={`${CHAT_COL} flex flex-col gap-5`}>
          {messages === null ? (
            <div className="flex h-40 items-center justify-center text-faint">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : historyError ? (
            <div className="py-16 text-center text-sm text-faint">
              {tr("view.historyError")}
            </div>
          ) : messages.length === 0 ? (
            <div className="py-16 text-center text-sm text-faint">
              {run.status === "running" ? tr("view.runningEmpty") : tr("view.empty")}
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
        </SubagentOpenOverride.Provider>
      </div>
    </div>
  );
}
