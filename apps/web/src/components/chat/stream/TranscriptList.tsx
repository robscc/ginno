"use client";

// TranscriptList — 消息列表渲染段（自 ChatStream.tsx 机械拆出，渲染逐字一致）：
// 系统行（context chips / 子代理卡片 / 拆分方案卡）、steer 带、用户气泡、
// 错误卡与 assistant 气泡的整段 map。委派卡折叠、handoff 分隔与「最后一条
// 可重试」判定也随渲染段搬到这里（同一 render 期计算，行为不变）。

import { Fragment, useCallback, useMemo, useState } from "react";
import type { HistoryTarget } from "@/lib/api/sessions";
import type { MutableRefObject, RefObject } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import {
  ContextBlocks,
  InnerBlocks,
  SteerBand,
  SubagentBlocks,
  UserBlocks,
  LastwordsCard,
  type Block,
} from "@/components/chat/blocks";
import { TaskEventBand } from "../TaskCard";
import { PlanUpdateCard } from "../blocks/subagentCards";
import { foldReportRows, isReportRow, reportAttention } from "../blocks/reportFold";
import { SubagentPlanCard } from "../subagentPlanCard";
import { TeamReplyCard, requestOpenMemberWorkspace } from "../TeamReplyCard";
import { stashOpenMemberIntent } from "@/components/team/openMemberIntent";
import { useRouter } from "next/navigation";
import {
  AssistantBubble,
  ErrorCard,
  HandoffDivider,
  TurnIdChip,
} from "../streamCards";
import type { ChatMsg } from "../streamCore";
import type { CompactionStage } from "../engine/handlers/11-context";
import { transcriptRowKeys } from "@/lib/sessionFind";
import { CopyBubbleButton } from "./CopyBubbleButton";
import { useMarkdownFileLinks } from "./useMarkdownFileLinks";
import type { SessionMeta } from "@/lib/types";
import { useTranscriptWindow, type ReadingAnchor, type TranscriptViewport } from "./useTranscriptWindow";
import type { HistoryLoadStatus } from "../engine/history";

function isIndependentEvent(block: Block): block is Extract<Block, { kind: "task_event" | "plan_update" }> {
  return block.kind === "task_event" || block.kind === "plan_update";
}

function IndependentEvents({ blocks }: { blocks: Block[] }) {
  return blocks.map((block, index) => !isIndependentEvent(block) ? null : (
    block.kind === "task_event"
      ? <TaskEventBand key={`task-${block.taskId}-${index}`} block={block} />
      : <PlanUpdateCard key={`plan-${block.planId}-${index}`} block={block} blockIndex={index} />
  ));
}

/** One projection for both find and transcript rendering, with original row addresses
 * preserved for windowing. Find row keys also cover replay rows without a message id. */
export function useTranscriptProjection(messages: ChatMsg[], sessionId?: string) {
  const g = useGinno();
  // 回报折叠（方案 A，2026-11-25）：紧跟助手消息的子代理结果/清单更新并入该
  // 消息尾部，与它的工具步骤合成同一个「执行过程」折叠组；失败、停止、等待
  // 回答的留在组外。纯逻辑在 reportFold（vitest 覆盖），行数与顺序不变，所以
  // 窗口化、分片挂载和消息锚点照旧按原列表寻址。
  const planItemsById = useMemo(() => {
    const byId = new Map<string, number>();
    for (const plan of g.plansBySession[sessionId ?? ""] ?? []) {
      byId.set(plan.plan_id, plan.items.length);
    }
    return byId;
  }, [g.plansBySession, sessionId]);
  const folded = useMemo(() => {
    const childStatus = new Map(g.sessions.map((s) => [s.id, s.subagent?.status]));
    return foldReportRows(messages, {
      attention: (block) =>
        reportAttention(
          block,
          block.kind === "subagent_result" ? childStatus.get(block.sessionId) : undefined,
        ),
      planTotal: (planId) => planItemsById.get(planId) ?? null,
    });
  }, [messages, g.sessions, planItemsById]);
  const rowKeys = useMemo(() => transcriptRowKeys(messages), [messages]);
  return useMemo(() => ({ folded, rowKeys }), [folded, rowKeys]);
}

export function TranscriptList({
  messages,
  projection,
  session,
  liveId,
  running,
  compactStage,
  serverRunning,
  wsStatus,
  parked,
  decideSubagentPlan,
  answerQuestion,
  editResend,
  dismissFailed,
  retryFailed,
  retryError,
  retryFromCheckpoint,
  dismissContext,
  scrollRef,
  stickRef,
  viewportRef,
  readingCache,
  historyStatus,
  focusLatestRef,
  onWindowEndChange,
  hasOlderHistory,
  loadHistory,
}: {
  messages: ChatMsg[];
  projection: ReturnType<typeof useTranscriptProjection>;
  session: SessionMeta | null;
  liveId: string | null;
  running: boolean;
  compactStage: CompactionStage | null;
  serverRunning: boolean;
  /** 本会话 WS 连接状态（ChatStream）：传给 live 气泡的等待行——断线期间
   *  改报连接中断，而不是让「等待下一轮」秒表空转。 */
  wsStatus?: "connecting" | "live" | "reconnecting" | "offline";
  /** turn 停车中（权限/提案/pending 问题）：抑制 live 气泡的「等待下一轮」
   *  行——停车由停靠卡/问题卡当信号，等待行空转会误导。 */
  parked?: boolean;
  decideSubagentPlan: (
    planId: string,
    decision: "confirm" | "cancel",
    subtasks?: import("@/lib/types").SubagentPlanSubtask[],
  ) => void;
  answerQuestion: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
  editResend: (msgId: string) => void;
  dismissFailed: (msgId: string) => void;
  retryFailed: (msgId: string) => void;
  retryError: (msgId: string) => void;
  retryFromCheckpoint: (msgId: string) => void;
  /** Hide one system notice in the current transcript. */
  dismissContext?: (msgId: string, block: Extract<Block, { kind: "context" }>) => void;
  scrollRef: RefObject<HTMLDivElement | null>;
  stickRef: MutableRefObject<boolean>;
  viewportRef: MutableRefObject<TranscriptViewport | null>;
  readingCache: Map<string, ReadingAnchor>;
  historyStatus: HistoryLoadStatus;
  focusLatestRef: MutableRefObject<string | null>;
  onWindowEndChange: (hasLater: boolean) => void;
  hasOlderHistory: boolean;
  loadHistory: (sid: string, target?: string, signal?: AbortSignal) => Promise<HistoryTarget | null>;
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
  const router = useRouter();
  const onFileLink = useMarkdownFileLinks(session);
  const { folded, rowKeys } = projection;
  const rows = folded.rows;
  const onInspectProcess = useCallback(() => {
    if (stickRef) stickRef.current = false;
  }, [stickRef]);
  // chat 域翻译；tr 为动态 key（failReason 稳定 key、状态枚举等）收敛后的
  // string 签名版本，存在性用 has 前置校验（理由见 blocks.tsx useChatT）。
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  // failReason 存稳定 key（attemptSend / engine 写入），渲染时翻译；
  // 未识别的值（未来来源）原样显示。
  const failText = (fr?: string) =>
    fr ? (tr.has(`sendFailed.${fr}`) ? tr(`sendFailed.${fr}`) : fr) : tc("sendFailed.unknownReason");

  const agentById = (id?: string | null) => g.agents.find((a) => a.id === id) ?? null;
  // Only the LAST failed/error bubble is retryable — re-invoking earlier ones
  // would re-insert stale user messages out of order in the server history.
  const lastRetryableId = messages.reduce<string | null>(
    (last, m) => ((m.status === "failed" || m.error) && m.sendPayload ? m.id : last),
    null,
  );
  // 最后一个 assistant 气泡：dev-swap 进度行（§4）在发起 turn 已结束
  // （非 streaming）时也挂在尾部持续显示，靠 isTail 定位。
  const lastAssistantId = messages.reduce<string | null>(
    (last, m) => (m.role === "assistant" && !m.error ? m.id : last),
    null,
  );
  // 委派状态 2026-10-13 上收为顶部 SubagentBoard（PlanBoard 同款）：转录流
  // 不再渲染 subagent_spawn 卡（组卡/单卡都不出）——清单模式的重复展示由此
  // 消除；结果卡/简报卡保留（职责不同）。

  // C+ 方案②「X 接手」分隔线：与上一条 agentId 已知的 assistant 气泡不同
  // agent 时插入。旧会话无标记（两侧任一 agentId 为空）自动跳过，不渲染。
  const handoffBefore: Record<number, true> = {};
  {
    let lastKnown: string | null = null;
    messages.forEach((m, i) => {
      if (m.role !== "assistant" || m.error) return;
      if (m.agentId && lastKnown && m.agentId !== lastKnown) handoffBefore[i] = true;
      if (m.agentId) lastKnown = m.agentId;
    });
  }

  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState(false);
  const { rendered, earlier, later, move, moveAfterLoad } = useTranscriptWindow({
    sessionId: session?.id, messages, folded, rowKeys, scrollRef, stickRef,
    viewportRef, readingCache, historyStatus, focusLatestRef, onWindowEndChange, loadHistory,
  });
  const historyButton = "rounded-full border border-line bg-card px-3 py-1 text-[11px] text-muted transition-colors hover:bg-elev focus-visible:outline focus-visible:outline-violet";

  return (
    <>
      {(earlier > 0 || hasOlderHistory) && (
        <div className="my-3 flex justify-center">
          <button type="button" className={historyButton} data-testid="load-earlier"
            disabled={loadingOlder}
            onClick={async (event) => {
              const focus = event.currentTarget === document.activeElement;
              if (earlier >= 60 || !hasOlderHistory || !session) {
                move(-1, focus);
                return;
              }
              setLoadingOlder(true);
              setOlderError(false);
              const reposition = moveAfterLoad(focus);
              try { await loadHistory(session.id); }
              catch { reposition(); setOlderError(true); }
              finally { setLoadingOlder(false); }
            }}>
            ↑ {loadingOlder ? tc("history.loading") : olderError ? tc("history.retryEarlier") :
              hasOlderHistory ? tc("history.earlier") : tc("loadEarlier", { hidden: earlier })}
          </button>
        </div>
      )}
      {rendered.map((rowIndex) => {
        const m = rows[rowIndex];
        const rowKey = rowKeys[rowIndex];
        const findAnchor = {
          "data-find-row": rowKey,
          "data-msg-id": m.id || undefined,
          "data-reading-anchor": m.history && m.id ? "" : undefined,
          tabIndex: -1,
        };
        return isReportRow(m) ? (
          // 带回报的系统行：走与助手气泡同一个折叠渲染器——没挂靠到助手消息的
          // 回报串在这里自成一行「执行过程」，需要用户处理的卡留在组外；与员工
          // 回传等合并在一条的回报，实时与回放因此折得一样。
          <div key={rowKey} {...findAnchor} className="mx-auto w-full max-w-[85%]">
            <InnerBlocks blocks={m.blocks} foldProcess onFileLink={onFileLink} onInspectProcess={onInspectProcess} />
          </div>
        ) : m.role === "system" ? (
          <div key={rowKey} {...findAnchor} className="flex flex-col items-center gap-2">
            <ContextBlocks
              blocks={m.blocks}
              onDismiss={
                dismissContext
                  ? (block) => dismissContext(m.id, block)
                  : undefined
              }
            />
            {/* 拆分方案卡片（P2）与子代理结果卡：与 context chips 同层——不是
                任何一方的气泡，是事件驱动的系统行。subagent_spawn 卡已停渲染
                （顶部 SubagentBoard 接管，见文件头注释）。 */}
            <div className="flex w-full max-w-[85%] flex-col gap-2">
              <IndependentEvents blocks={m.blocks} />
              {m.blocks
                .filter((b): b is Extract<Block, { kind: "subagent_plan" }> => b.kind === "subagent_plan")
                .map((b) => (
                  <SubagentPlanCard key={b.planId} block={b} onDecide={decideSubagentPlan} />
                ))}
              <SubagentBlocks
                sourceBlocks={m.blocks}
                blocks={m.blocks.filter(
                  (b): b is Extract<Block, { kind: "subagent_result" }> =>
                    b.kind === "subagent_result",
                )}
              />
              {/* worker 协议信封卡（live 吸收路径 handle.ts 折出的 system 行） */}
              <SubagentBlocks
                sourceBlocks={m.blocks}
                blocks={m.blocks.filter(
                  (
                    b,
                  ): b is Extract<
                    Block,
                    { kind: "worker_result" | "worker_assign" }
                  > =>
                    b.kind === "worker_result" || b.kind === "worker_assign",
                )}
              />
            </div>
          </div>
        ) : m.role === "user" && m.blocks.every(isIndependentEvent) && m.blocks.length > 0 ? (
          <div key={rowKey} {...findAnchor} className="mx-auto flex w-full max-w-[85%] flex-col gap-2">
            <IndependentEvents blocks={m.blocks} />
          </div>
        ) : m.role === "user" &&
          m.blocks.some(
            (b) =>
              b.kind === "subagent_result" ||
              b.kind === "subagent_brief" ||
              b.kind === "worker_result" ||
              b.kind === "worker_assign",
          ) ? (
          // runtime 注入的子代理结果（契约 3）与子会话首条任务简报
          // （<ginno_subagent_brief>）在历史重放中是 HumanMessage，重载后以
          // user 角色回来：折成卡片系统行渲染，而不是落到 UserBlocks
          // （它不认识这些块，会渲染成空气泡）。worker 协议信封
          // （<ginno_worker_result>/<ginno_worker_assign>）同通道同处理。
          <div key={rowKey} {...findAnchor} className="flex flex-col items-center gap-2">
            <div className="flex w-full max-w-[85%] flex-col gap-2">
              <IndependentEvents blocks={m.blocks} />
              <SubagentBlocks
                sourceBlocks={m.blocks}
                session={session?.type === "subagent" ? session : undefined}
                blocks={m.blocks.filter(
                  (
                    b,
                  ): b is Extract<
                    Block,
                    | { kind: "subagent_result" }
                    | { kind: "subagent_brief" }
                    | { kind: "worker_result" }
                    | { kind: "worker_assign" }
                  > =>
                    b.kind === "subagent_result" ||
                    b.kind === "subagent_brief" ||
                    b.kind === "worker_result" ||
                    b.kind === "worker_assign",
                )}
              />
            </div>
          </div>
        ) : m.role === "user" && m.blocks.some((b) => b.kind === "steer") ? (
          // A steered message with no assistant step to attach to (absorbed
          // by the turn's first model request, or the transcript ended on
          // it): a full-width band, not a right-aligned bubble — it is not a
          // turn of its own (design §4.2).
          <div key={rowKey} {...findAnchor} className="flex flex-col gap-1">
            {m.blocks
              .map((b, index) => b.kind === "steer" && (
                <div key={b.steerId ?? index} data-block-idx={index}><SteerBand block={b} /></div>
              ))}
          </div>
        ) : m.role === "user" &&
          m.blocks.some((b) => b.kind === "text" && b.text.trimStart().startsWith("[lastwords]")) ? (
          // 遗言信封（2026-11-19）：机器注入的 [lastwords] 文本——全宽系统卡，
          // 不落右对齐用户气泡（见 blocks.LastwordsCard 注释）。
          <div key={rowKey} {...findAnchor} className="flex flex-col items-center gap-2">
            <div className="flex w-full justify-center">
              <LastwordsCard
                text={
                  m.blocks.find(
                    (b): b is Extract<Block, { kind: "text" }> =>
                      b.kind === "text" && b.text.trimStart().startsWith("[lastwords]"),
                  )?.text ?? ""
                }
              />
            </div>
            <TurnIdChip turnId={m.turnId} />
          </div>
        ) : m.role === "user" ? (
          <div key={rowKey} {...findAnchor} className="flex flex-col items-end gap-1">
            <TurnIdChip turnId={m.turnId} />
            {/* w-full (not max-w-full): the row width must be definite so the
                bubble's max-w-[78%] resolves against the column, not against
                the row's own shrink-to-fit width — otherwise the percentage
                collapses the bubble and the text overflows to the right. */}
            <div className="group flex w-full items-center justify-end gap-2">
              {m.status === "failed" && (
                <div className="flex shrink-0 items-center gap-1.5">
                  {m.id === lastRetryableId && (
                    <>
                      <button
                        onClick={() => editResend(m.id)}
                        className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted opacity-0 transition-opacity hover:text-txt group-hover:opacity-100"
                      >
                        {tc("sendFailed.editResend")}
                      </button>
                      <button
                        onClick={() => dismissFailed(m.id)}
                        className="rounded-md border border-line2 px-1.5 py-0.5 text-[10px] text-muted opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
                      >
                        {tc("sendFailed.delete")}
                      </button>
                    </>
                  )}
                  {m.id === lastRetryableId ? (
                    <button
                      onClick={() => retryFailed(m.id)}
                      title={tc("sendFailed.titleRetry", { reason: failText(m.failReason) })}
                      aria-label={tc("sendFailed.ariaRetry")}
                      className="shrink-0 transition-transform hover:scale-110"
                    >
                      <AlertCircle className="h-[18px] w-[18px] text-red" />
                    </button>
                  ) : (
                    <span title={tc("sendFailed.title", { reason: failText(m.failReason) })}>
                      <AlertCircle className="h-[18px] w-[18px] shrink-0 text-red/50" />
                    </span>
                  )}
                </div>
              )}
              {m.status === "sending" && (
                <Loader2 className="h-4 w-4 shrink-0 animate-spin text-faint" aria-label={tc("composer.sendingAria")} />
              )}
              {/* 用户气泡的复制按钮：与失败重发动作同一 hover 透明度语言，
                  无可复制文本（纯附件）时不渲染 */}
              <CopyBubbleButton
                blocks={m.blocks}
                className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
              />
              <div
                className={`max-w-[78%] rounded-2xl rounded-tr-md border px-4 py-2.5 text-sm leading-relaxed ${
                  m.status === "failed"
                    ? "border-red/40 bg-card2/60 text-muted"
                    : "border-line bg-card2 text-txt"
                }`}
              >
                <UserBlocks blocks={m.blocks} />
              </div>
            </div>
            {m.status === "failed" && (
              <div className="selectable text-[10px] text-red/80">
                {tc("sendFailed.label")}{m.failReason ? `: ${failText(m.failReason)}` : ""}
                {m.id === lastRetryableId && tc("sendFailed.retryHint")}
              </div>
            )}
            {/* AgentTeam 投递回执 sysline（backlog P1-1）。三态语义严格区分：
                stalled **也已发送**，只给「进入工作台」不给重发（重发=重复投递）；
                blocked 原子拒绝一个字都没发，引导去代答而不是重发。 */}
            {m.teamDelivery && (
              <div
                className={`selectable flex items-center gap-1.5 text-[10px] ${
                  m.teamDelivery.receipt === "failed"
                    ? "text-red/80"
                    : m.teamDelivery.receipt === "stalled"
                      ? "text-yellow/90"
                      : "text-faint"
                }`}
              >
                <span>
                  {m.teamDelivery.receipt === "delivered"
                    ? tc("team.receiptDelivered", {
                        name: m.teamDelivery.displayName || "",
                        channel: m.teamDelivery.channel === "queue"
                          ? tc("team.receiptQueued")
                          : m.teamDelivery.channel === "steer"
                            ? tc("team.receiptSteer")
                            : "",
                      })
                    : m.teamDelivery.receipt === "stalled"
                      ? tc("team.receiptStalled", { name: m.teamDelivery.displayName || "" })
                      : m.teamDelivery.reason === "agent_blocked"
                        ? tc("team.receiptBlocked", { name: m.teamDelivery.displayName || "" })
                        : tc("team.receiptFailed", {
                            name: m.teamDelivery.displayName || "",
                            reason: m.teamDelivery.error || m.teamDelivery.reason || "",
                          })}
                </span>
                {/* blocked / stalled 都给「进入工作台」——去处理卡点或看进度，
                    而不是重发。failed 的其他原因走上面的通用重试入口。 */}
                {(m.teamDelivery.receipt === "stalled"
                  || m.teamDelivery.reason === "agent_blocked")
                  && m.teamDelivery.memberSessionId && (
                  <button
                    onClick={() => {
                      const d = m.teamDelivery!;
                      // 与回帖卡同一范式（P2-25）：不在 /team 先存一次性意图
                      // 再导航，否则纯 window 事件只有 /team 在监听会静默丢失。
                      if (window.location.pathname !== "/team") {
                        stashOpenMemberIntent({
                          teamId: d.teamId,
                          instanceId: d.instanceId || "",
                          sessionId: d.memberSessionId || "",
                        });
                        router.push("/team");
                      }
                      requestOpenMemberWorkspace(
                        d.memberSessionId || "", d.instanceId || "", d.teamId,
                      );
                    }}
                    className="rounded border border-line2 px-1.5 py-0.5 text-[10px] text-muted transition-colors hover:text-txt"
                  >
                    {tc("team.receiptOpenWorkspace")}
                  </button>
                )}
              </div>
            )}
          </div>
        ) : m.error ? (
          <div key={rowKey} {...findAnchor} className="min-w-0 rounded-xl">
            <ErrorCard
              message={m.blocks[0]?.kind === "text" ? m.blocks[0].text : ""}
              turnId={m.turnId}
              canRetry={!!m.sendPayload && m.id === lastRetryableId}
              busy={running}
              onRetry={() => retryError(m.id)}
              onRetryFromCheckpoint={() => retryFromCheckpoint(m.id)}
            />
          </div>
        ) : (
          <Fragment key={rowKey}>
            {handoffBefore[rowIndex] && (
              <HandoffDivider agent={agentById(m.agentId)} agentName={m.agentName} />
            )}
            <div {...findAnchor} className="min-w-0 rounded-xl">
            {m.teamReply ? (
              // AgentTeam 成员回帖（§5.2 要点 2）：渲染成回帖卡而非普通气泡。
              <TeamReplyCard meta={m.teamReply} agentId={m.agentId} blocks={m.blocks} />
            ) : (
            <AssistantBubble
              agent={agentById(m.agentId)}
              agentName={m.agentName}
              subagentTypeName={
                session?.type === "subagent"
                  ? (session.subagent as { agent_type?: string } | undefined)?.agent_type
                  : undefined
              }
              blocks={m.blocks}
              streaming={m.id === liveId}
              statusOverride={compactStage && m.id === (liveId ?? lastAssistantId)
                ? tc(compactStage === "writing" ? "stream.compactWriting" : "stream.compacting")
                : null}
              connState={wsStatus ?? null}
              parkedTurn={parked}
              turnId={m.turnId}
              failed={m.failed}
              ts={m.ts}
              isTail={m.id === lastAssistantId}
              isLatestTurn={m.id === lastAssistantId}
              sessionKey={session?.id ?? null}
              onFileLink={onFileLink}
              onInspectProcess={onInspectProcess}
              onAnswerQuestion={answerQuestion}
              // questionLive: is this session's turn actually alive? A
              // parked question keeps the server turn running WITHOUT a
              // message.end, so two signals cover it: `liveId` (this client
              // is watching the stream) and `serverRunning` (the answer to
              // the turn_state probe, which is the ONLY one that survives a
              // full reload — history rebuilds the card as pending, and
              // without the probe it would render disabled even though the
              // backend would accept the answer).
              // Deliberately not `running`: its questionPending term comes
              // from the card itself (circular — a stale pending card would
              // keep itself interactive forever).
              // 渠道会话（钉钉/企微）整体只读：提问在 IM 侧作答（tap 文本化），
              // 桌面卡片只展示——不给答题入口，避免两边抢答同一停车。
              questionLive={
                (liveId !== null || serverRunning) && session?.type !== "channel"
              }
            />
            )}
            </div>
          </Fragment>
        );
      })}
      {later > 0 && (
        <div className="my-3 flex justify-center">
          <button type="button" className={historyButton} data-testid="load-later"
            onClick={(event) => move(1, event.currentTarget === document.activeElement)}>
            ↓ {tc("loadLater", { hidden: later })}
          </button>
        </div>
      )}
    </>
  );
}
