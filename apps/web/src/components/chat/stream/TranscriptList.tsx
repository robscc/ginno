"use client";

// TranscriptList — 消息列表渲染段（自 ChatStream.tsx 机械拆出，渲染逐字一致）：
// 系统行（context chips / 子代理卡片 / 拆分方案卡）、steer 带、用户气泡、
// 错误卡与 assistant 气泡的整段 map。委派卡折叠、handoff 分隔与「最后一条
// 可重试」判定也随渲染段搬到这里（同一 render 期计算，行为不变）。

import { Fragment } from "react";
import { AlertCircle, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import {
  ContextBlocks,
  SteerBand,
  SubagentBlocks,
  UserBlocks,
  type Block,
} from "@/components/chat/blocks";
import { SubagentPlanCard } from "../subagentPlanCard";
import {
  AssistantBubble,
  ErrorCard,
  HandoffDivider,
  TurnIdChip,
} from "../streamCards";
import { type ChatMsg } from "../streamCore";
import type { SessionMeta } from "@/lib/types";

export function TranscriptList({
  messages,
  session,
  liveId,
  running,
  compacting,
  serverRunning,
  decideSubagentPlan,
  answerQuestion,
  editResend,
  dismissFailed,
  retryFailed,
  retryError,
  retryFromCheckpoint,
}: {
  messages: ChatMsg[];
  session: SessionMeta | null;
  liveId: string | null;
  running: boolean;
  compacting: boolean;
  serverRunning: boolean;
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
}) {
  const g = useGinno();
  const tc = useTranslations("chat");
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

  return (
    <>
      {messages.map((m, idx) =>
        m.role === "system" ? (
          <div key={m.id} className="flex flex-col items-center gap-2">
            <ContextBlocks
              blocks={m.blocks.filter((b): b is Extract<Block, { kind: "context" }> => b.kind === "context")}
            />
            {/* 拆分方案卡片（P2）与子代理结果卡：与 context chips 同层——不是
                任何一方的气泡，是事件驱动的系统行。subagent_spawn 卡已停渲染
                （顶部 SubagentBoard 接管，见文件头注释）。 */}
            <div className="flex w-full max-w-[85%] flex-col gap-2">
              {m.blocks
                .filter((b): b is Extract<Block, { kind: "subagent_plan" }> => b.kind === "subagent_plan")
                .map((b) => (
                  <SubagentPlanCard key={b.planId} block={b} onDecide={decideSubagentPlan} />
                ))}
              <SubagentBlocks
                blocks={m.blocks.filter(
                  (b): b is Extract<Block, { kind: "subagent_result" }> =>
                    b.kind === "subagent_result",
                )}
              />
            </div>
          </div>
        ) : m.role === "user" &&
          m.blocks.some(
            (b) => b.kind === "subagent_result" || b.kind === "subagent_brief",
          ) ? (
          // runtime 注入的子代理结果（契约 3）与子会话首条任务简报
          // （<ginno_subagent_brief>）在历史重放中是 HumanMessage，重载后以
          // user 角色回来：折成卡片系统行渲染，而不是落到 UserBlocks
          // （它不认识这些块，会渲染成空气泡）。
          <div key={m.id} className="flex flex-col items-center gap-2">
            <div className="w-full max-w-[85%]">
              <SubagentBlocks
                session={session?.type === "subagent" ? session : undefined}
                blocks={m.blocks.filter(
                  (
                    b,
                  ): b is Extract<
                    Block,
                    { kind: "subagent_result" | "subagent_brief" }
                  > =>
                    b.kind === "subagent_result" || b.kind === "subagent_brief",
                )}
              />
            </div>
          </div>
        ) : m.role === "user" && m.blocks.some((b) => b.kind === "steer") ? (
          // A steered message with no assistant step to attach to (absorbed
          // by the turn's first model request, or the transcript ended on
          // it): a full-width band, not a right-aligned bubble — it is not a
          // turn of its own (design §4.2).
          <div key={m.id} className="flex flex-col gap-1">
            {m.blocks
              .filter((b): b is Extract<Block, { kind: "steer" }> => b.kind === "steer")
              .map((b) => (
                <SteerBand key={b.steerId ?? m.id} block={b} />
              ))}
          </div>
        ) : m.role === "user" ? (
          <div key={m.id} className="flex flex-col items-end gap-1">
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
              <div className="text-[10px] text-red/80">
                {tc("sendFailed.label")}{m.failReason ? `: ${failText(m.failReason)}` : ""}
                {m.id === lastRetryableId && tc("sendFailed.retryHint")}
              </div>
            )}
          </div>
        ) : m.error ? (
          <ErrorCard
            key={m.id}
            message={m.blocks[0]?.kind === "text" ? m.blocks[0].text : ""}
            turnId={m.turnId}
            canRetry={!!m.sendPayload && m.id === lastRetryableId}
            busy={running}
            onRetry={() => retryError(m.id)}
            onRetryFromCheckpoint={() => retryFromCheckpoint(m.id)}
          />
        ) : (
          <Fragment key={m.id}>
            {handoffBefore[idx] && (
              <HandoffDivider agent={agentById(m.agentId)} agentName={m.agentName} />
            )}
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
              statusOverride={compacting ? tc("stream.compacting") : null}
              turnId={m.turnId}
              failed={m.failed}
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
              questionLive={liveId !== null || serverRunning}
            />
          </Fragment>
        ),
      )}
    </>
  );
}
