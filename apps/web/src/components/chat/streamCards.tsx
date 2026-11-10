"use client";

// streamCards — ChatStream 的气泡级卡片组件（自 ChatStream.tsx 机械拆出，
// 行为零变化）：turnId chip、子会话顶栏、DSL 提案卡、错误卡、接手分隔线、
// 助手气泡与加载动画点。全部通过 props 取数，不依赖 ChatStream 内部状态。

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronDown, FileEdit, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import { relTime } from "@/lib/utils";
import { useTranslations } from "next-intl";
import { InnerBlocks, RefBlocks, subagentStatusMeta, SubagentKindBadges, useStatusLabel, type Block } from "@/components/chat/blocks";
import { innerTail, isWaitingNextRound } from "./waitState";
import { useDevSwapProgress } from "@/lib/useDevSwapProgress";
import { expectationFor, waitLineData, fmtClock } from "@/lib/devswap/stats";
import { copyText } from "./copyText";
import { CopyBubbleButton } from "./stream/CopyBubbleButton";
import { DiffView } from "@/components/workflow/DiffView";
import type { AgentConfig, SessionMeta } from "@/lib/types";
import type { VersionPropose } from "./streamCore";

/** chat 域翻译 + 动态 key 收敛转型（本文件共用；理由见 blocks.tsx useChatT）。 */
function useChatT() {
  const tc = useTranslations("chat");
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  return { tc, tr };
}

// 剪贴板助手抽到共享模块（copyText.ts）：TurnIdChip 与气泡「复制内容」
// 按钮（CopyBubbleButton）共用，含非安全上下文的 execCommand 回退。
/** Click-to-copy per-turn trace UUID. The full id is what you grep the sidecar
 *  logs for (`turn=...`); we show a short prefix to keep the bubble tidy. */
export function TurnIdChip({ turnId }: { turnId?: string }) {
const [copied, setCopied] = useState(false);
const { tc } = useChatT();
if (!turnId) return null;
const short = turnId.slice(0, 8);
return (
  <button
    onClick={async () => {
      if (await copyText(turnId)) {
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }
    }}
    title={tc("turnId.title", { id: turnId })}
    className="rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
  >
    {copied ? tc("turnId.copied") : `#${short}`}
  </button>
);
}
/** workflow_propose_edit diff confirmation card (workflow-ux-redesign P0
 *  polish): busy buttons + collapsed-by-default diff with hunk count. The
 *  session graph is paused at the tool's interrupt until the user decides. */
/** 子会话视图顶栏（subagent-design.md §6.2）：goal / 约束 / 验收 / 状态 /
 *  「返回主对话」。只在 type==="subagent" 的会话渲染；状态从 store 实时读，
 *  subagent.status 事件换挡时无需额外订阅。 */
export function SubagentTopBar({ session }: { session: SessionMeta }) {
const g = useGinno();
const { tc } = useChatT();
const statusLabel = useStatusLabel();
const live = g.sessions.find((s) => s.id === session.id) ?? session;
const sub = live.subagent;
if (!sub) return null;
const meta = subagentStatusMeta(sub.status);
const active = sub.status === "running" || sub.status === "waiting";
const back = () => {
  if (live.parent_session_id) g.setActiveSession(live.parent_session_id);
  // 2026-11-05：无 parent（Workflows 页入口）时回最近会话——不再禁用死路
  else {
    const last = g.sessions.find((s) => s.id === g.activeSessionId) ?? g.sessions[0];
    if (last) g.setActiveSession(last.id);
  }
};
return (
  <div className="border-b border-line bg-panel/60 px-6 py-2 text-xs">
    <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2">
      <span className="shrink-0">🤖</span>
      <span className="min-w-0 max-w-[40%] truncate font-medium text-txt" title={sub.goal}>
        {sub.goal || tc("subagent.fallbackTitle")}
      </span>
      <SubagentKindBadges sub={sub} />
      {active && (
        <span
          className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full"
          style={{ background: meta.color }}
        />
      )}
      <span className="shrink-0 text-muted" title={tc("status.tooltip", { status: statusLabel(sub.status) })}>
        {meta.glyph} {statusLabel(sub.status)}
      </span>
      {sub.constraints && (
        <span className="min-w-0 max-w-[24%] truncate text-faint" title={`${tc("subagent.constraintsLabel")}${sub.constraints}`}>
          {tc("subagent.constraintsLabel")}{sub.constraints}
        </span>
      )}
      {sub.acceptance && (
        <span className="min-w-0 max-w-[24%] truncate text-faint" title={`${tc("subagent.acceptanceLabel")}${sub.acceptance}`}>
          {tc("subagent.acceptanceLabel")}{sub.acceptance}
        </span>
      )}
      {/* 2026-11-05：无 parent（Workflows 入口）时 back 内部兜底回最近会话 */}
      <button
        onClick={back}
        className="ml-auto shrink-0 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:opacity-40"
      >
        {tc("subagent.backToMain")}
      </button>
    </div>
  </div>
);
}

export function ProposeCard({
propose,
onDecide,
}: {
propose: VersionPropose;
onDecide: (decision: "allow" | "deny") => void;
}) {
const [busy, setBusy] = useState<null | "allow" | "deny">(null);
const [diffOpen, setDiffOpen] = useState(false);
const { tc } = useChatT();
const hunks = (propose.diff.match(/^@@/gm) || []).length;
const decide = (d: "allow" | "deny") => {
  if (busy) return;
  setBusy(d);
  onDecide(d); // the card unmounts when the server clears the pending propose
};
return (
  <div className="mx-auto w-full max-w-3xl px-6">
    <div className="mb-2 rounded-xl border border-yellow/30 bg-yellow/[0.04] p-3">
      <div className="mb-1 flex items-center gap-2 text-sm font-medium text-yellow">
        <FileEdit className="h-3.5 w-3.5" />
        {tc("propose.title")}
        <span className="rounded border border-yellow/40 px-1.5 py-0.5 text-[10px] font-normal text-muted">
          {tc("propose.versionBadge", { id: propose.workflow_id, version: propose.from_version })}
        </span>
      </div>
      {propose.rationale && (
        <div className="selectable mb-2 text-xs text-muted">{tc("propose.rationale", { rationale: propose.rationale })}</div>
      )}
      <button
        onClick={() => setDiffOpen((v) => !v)}
        className="mb-2 flex items-center gap-1 text-[11px] text-faint hover:text-muted"
      >
        <ChevronDown className={`h-3 w-3 transition-transform ${diffOpen ? "" : "-rotate-90"}`} />
        {diffOpen ? tc("propose.collapseDiff") : tc("propose.viewDiff", { count: hunks })}
      </button>
      {diffOpen && <DiffView diff={propose.diff} />}
      <div className="mt-3 flex gap-2">
        <button
          onClick={() => decide("allow")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy === "allow" && <Loader2 className="h-3 w-3 animate-spin" />}
          {busy === "allow" ? tc("propose.applying") : tc("propose.apply")}
        </button>
        <button
          onClick={() => decide("deny")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
        >
          {busy === "deny" && <Loader2 className="h-3 w-3 animate-spin" />}
          {tc("propose.reject")}
        </button>
      </div>
    </div>
  </div>
);
}

/** Turn failed at runtime: the input was delivered, the run errored (model /
 * provider failure, stall watchdog, …). Rendered as a dedicated red card with
 * a retry action instead of a plain "[error]" text bubble. */
export function ErrorCard({
message,
turnId,
canRetry,
busy,
onRetry,
onRetryFromCheckpoint,
}: {
message: string;
turnId?: string;
canRetry: boolean;
busy?: boolean;
onRetry: () => void;
onRetryFromCheckpoint?: () => void;
}) {
const { tc } = useChatT();
return (
  <div className="rounded-xl border border-red/40 bg-red/10 px-4 py-3">
    <div className="mb-1.5 flex items-center gap-2">
      <AlertCircle className="h-4 w-4 shrink-0 text-red" />
      <span className="text-sm font-medium text-red">{tc("errorCard.title")}</span>
      <span className="ml-auto">
        <TurnIdChip turnId={turnId} />
      </span>
    </div>
    <pre className="mb-3 max-h-32 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted">
      {message}
    </pre>
    {canRetry && (
      <div className="flex gap-2">
        <button
          onClick={onRetry}
          disabled={busy}
          title={tc("errorCard.retryTitle")}
          className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {tc("errorCard.retry")}
        </button>
        {onRetryFromCheckpoint && (
          <button
            onClick={onRetryFromCheckpoint}
            disabled={busy}
            title={tc("errorCard.resumeTitle")}
            className="rounded-lg border border-violet/40 bg-violet/10 px-3 py-1.5 text-xs font-medium text-violet transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {tc("errorCard.resume")}
          </button>
        )}
      </div>
    )}
  </div>
);
}

/** C+ 方案②：虚线 pill 分隔线——下一个气泡由哪个 agent 接手。 */
export function HandoffDivider({ agent, agentName }: { agent: AgentConfig | null; agentName?: string }) {
const { tc } = useChatT();
const hex = agentHex(agent?.color);
const name = agent?.name || agentName || tc("stream.fallbackAgent");
return (
  <div className="-my-1 flex items-center gap-2 text-xs">
    <div className="h-px flex-1 border-t border-dashed border-line" />
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-dashed bg-card px-2.5 py-0.5"
      style={{ borderColor: hex + "55", color: hex }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
      {tc("handoff.takesOver", { name })}
    </span>
    <div className="h-px flex-1 border-t border-dashed border-line" />
  </div>
);
}

export function AssistantBubble({
  subagentTypeName,
agent,
agentName,
blocks,
streaming,
statusOverride,
turnId,
failed,
ts,
onAnswerQuestion,
questionLive,
connState,
parkedTurn,
isTail,
sessionKey,
}: {
agent: AgentConfig | null;
agentName?: string;
/** 子代理类型名（子会话视图传入）：优先于继承来的 persona 名显示 */
subagentTypeName?: string;
blocks: Block[];
streaming?: boolean;
/** 瞬态状态行文案（如「压缩中…」）：非空时优先于 connecting/thinking 计时文案；
 *  有内容（hasInner）的流式气泡上头部状态行不渲染，此文案由尾部等待行承接 */
statusOverride?: string | null;
turnId?: string;
failed?: boolean;
// 完成态相对时间的源（unix 秒，message.end 时盖戳，见 handle.ts）：无 ts
// （历史重放没有时间戳源）则不渲染时间标签，也不再说「刚刚」的谎。
ts?: number;
// ask_user resume channel (see InnerBlocks): a parked question card is
// answered from inside the bubble, not from a bottom-docked prompt.
onAnswerQuestion?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
questionLive?: boolean;
/** 本会话 WS 连接状态（ChatStream 的 wsStatus）：live 气泡等待行的兜底信号——
 *  reconnecting/offline 期间流事件根本到不了，等待行改报连接中断，而不是
 *  让「等待下一轮」的秒表空转骗人（用户反馈 2026-11-03：看着像卡死）。 */
connState?: "connecting" | "live" | "reconnecting" | "offline" | null;
/** turn 停车中（权限/提案等停靠卡在等用户决定，ChatStream 的 parked）：此时
 *  尾块往往也是已结束的工具，但「等待下一轮」会空转误导——停车由停靠卡
 *  自身当信号，等待行让位。pending 问题块已在 tail 判据里排除。 */
parkedTurn?: boolean;
/** 本气泡是否转录流中最后一个 assistant 气泡：dev-swap 进度行（§4）在
 *  发起 turn 已结束（非 streaming）时也挂在尾部持续显示，靠它定位。 */
isTail?: boolean;
/** 当前会话 id：dev-swap 进度行/结果落块的会话门控（§4.5 B 会话不打扰）。 */
sessionKey?: string | null;
}) {
const { tc } = useChatT();
// devswap 域文案（动态阶段 key 收敛，cast 理由同 useChatT）。
const tds = useTranslations("devswap") as unknown as (
  key: string,
  values?: Record<string, string | number>,
) => string;
const hex = agentHex(agent?.color);
// 子会话的 persona 是继承父会话的（Dev Agent），标注没有信息量——子代理类型
// 才是它的身份，与侧栏行/子会话顶栏/主对话卡片保持一致（用户反馈 2026-10-01）。
const displayName = subagentTypeName || agent?.name || agentName || tc("stream.fallbackAgent");
const hasInner = blocks.some((b) => b.kind !== "ref" && b.kind !== "turn_meta");

// ── 轮次边界等待行（2026-11-03 用户反馈）─────────────────────────────────────
// 多步 turn 里「上一个块已收尾 → 下一轮首个 delta」之间是模型调用静默窗（慢
// 网关可达十几秒）：气泡已有内容时没有任何活性指示，看着像卡死。判据是纯
// 函数（waitState.ts，供 node 测试）：尾块已收尾且未停车时，在气泡尾部给
// 一条带秒表的活性行。
const waitingNextRound = isWaitingNextRound(blocks, streaming, parkedTurn);
const connLost = connState === "reconnecting" || connState === "offline";
const tail = innerTail(blocks);
// 秒表计「尾部静默了多久」：任何流事件改动尾部（tail 引用变化）即归零，静默
// 期每秒 +1——数字在走本身就是「没卡住」的信号。
const [waitElapsed, setWaitElapsed] = useState(0);
useEffect(() => {
  setWaitElapsed(0);
}, [tail]);
useEffect(() => {
  if (!waitingNextRound) return;
  const timer = setInterval(() => setWaitElapsed((e) => e + 1), 1000);
  return () => clearInterval(timer);
}, [waitingNextRound]);
const waitText = useMemo(() => {
  // 优先级：连接中断 > 瞬态覆盖（压缩中）> 泛化等待。断线时事件根本到不了，
  // 秒表空转会让人误以为在等下一轮。
  if (connLost)
    return connState === "offline"
      ? tc("stream.connLostOffline")
      : tc("stream.connLostReconnecting");
  if (statusOverride) return statusOverride;
  return tc("stream.waitNextRound", { seconds: waitElapsed });
}, [connLost, connState, statusOverride, waitElapsed, tc]);

// ── dev-swap 进度倒计时（dev-swap-progress-design.md §4）───────────────────
// /dev-swap 是后台任务：发起 turn 秒结，进度行不能依赖 streaming——run 进行
// 中挂在发起会话最后一个气泡尾部持续显示（isTail，不限 streaming），阶段
// 推进原地刷新；该会话的 streaming 等待位也优先显示进度。其他会话不打扰
// （§4.5 sid 门控，终端 make dev-swap 无 sid → 只看弹窗卡片）。
// EMA/P90 来自 ginno:devswap-stats（前端 localStorage，devswap/stats.ts）。
const { run: dsRun } = useDevSwapProgress();
const dsActive = dsRun?.state === "running";
const dsOwn =
  !!dsActive && !!dsRun?.sid && sessionKey != null && dsRun.sid === sessionKey;
const [dsTick, setDsTick] = useState(0);
useEffect(() => {
  if (!dsActive) return;
  const timer = setInterval(() => setDsTick((n) => n + 1), 1000);
  return () => clearInterval(timer);
}, [dsActive]);
const dsLine = useMemo(() => {
  if (!dsActive || !dsRun) return null;
  void dsTick; // 秒跳重渲染源
  const stageElapsed = Math.max(
    0,
    Math.floor(
      Date.now() / 1000 -
        // stageStartedAt：bus 维护的阶段起始锚，事件 ts 会反复刷新
        (dsRun.stageStartedAt ?? dsRun.ts ?? Math.floor(Date.now() / 1000)),
    ),
  );
  const exp = expectationFor(dsRun.stage);
  const d = waitLineData(stageElapsed, dsRun.pct ?? null, exp);
  const stage = tds(`card.stage.${dsRun.stage}`);
  return {
    slow: d.slow,
    text: d.slow
      ? tds("wait.slow", { stage, p90: fmtClock(exp.p90) })
      : tds("wait.line", {
          stage,
          bar: d.bar,
          pct: d.pctLabel,
          elapsed: d.elapsedLabel,
          eta: d.etaLabel,
        }),
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
}, [dsActive, dsRun, dsTick]);
// 尾部持续进度行：发起会话 + 最后一个气泡，run 进行中一直挂着；finish 阶段
// 壳重启断 WS 属预期流程，此行不让位给断线文案。
const dsTail = dsOwn && !!isTail && !!dsLine;
const dsLabelOn = dsTail || (dsOwn && !connLost && streaming);
const lineVisible =
  (streaming && (connLost || waitingNextRound || !!statusOverride || dsActive)) || dsTail;
// 优先级：进度行（dsOwn）> 连接中断 > 瞬态覆盖 > 泛化等待。
const tailLabel = dsLabelOn ? (dsLine?.text ?? waitText) : waitText;
const tailAmber = dsLabelOn && !!dsLine?.slow;

// Track elapsed time for dynamic status text during TTFT wait
const [elapsed, setElapsed] = useState(0);
useEffect(() => {
  if (!streaming || hasInner) {
    setElapsed(0);
    return;
  }
  setElapsed(0);
  const timer = setInterval(() => setElapsed((e) => e + 1), 1000);
  return () => clearInterval(timer);
}, [streaming, hasInner]);

// Dynamic status text based on elapsed time
const statusText = useMemo(() => {
  if (statusOverride) return statusOverride;
  if (elapsed < 2) return tc("stream.connecting");
  if (elapsed < 10) return tc("stream.thinking");
  return tc("stream.stillThinking", { seconds: elapsed });
}, [statusOverride, elapsed, tc]);
// 完成态相对时间要跟着墙钟走（2026-10-29 用户反馈：1h 后气泡仍显示「刚刚」）：
// relTime 只在重渲染时重算，闲置会话没有天然的重渲染源——60s 一跳强制刷新
// （relTime 最细粒度是分钟，60s 足够）；无 ts 不起定时器。
const [, setRelTick] = useState(0);
useEffect(() => {
  if (streaming || !ts) return;
  const timer = setInterval(() => setRelTick((n) => n + 1), 60_000);
  return () => clearInterval(timer);
}, [streaming, ts]);
// 流式 → 状态文案；完成 → 相对时间（收口时刻起算）；无 ts（历史重放）→
// 不渲染。旧的硬编码「刚刚」会说一小时的谎，宁可空着。
// 已有内容的流式气泡头部不再显示状态行：elapsed 只在空气泡时计时，旧行为
// 会把「正在连接模型……」冻在整个流式期；活性信号改由尾部等待行承担。
const timeLabel = streaming ? (hasInner ? "" : statusText) : ts ? relTime(ts) : "";
return (
  <div className="min-w-0">
    {/* C+ 方案②归属徽标：agent 色 dot + 名字 pill（原型风格），替代原先的
        头像方块——归属信息一眼可见，且与「X 接手」分隔线同一视觉语言。 */}
    <div className="mb-1 flex items-center gap-2">
      <span
        className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium"
        style={{ borderColor: hex + "55", background: hex + "1a", color: hex }}
        title={displayName}
      >
        <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
        {displayName}
      </span>
      {timeLabel ? <span className="text-xs text-faint">{timeLabel}</span> : null}
      <span className="ml-auto flex items-center gap-1.5">
        {/* 每个气泡一个复制按钮（2026-10-26）：与 turnId chip 同层，常驻但低调 */}
        <CopyBubbleButton blocks={blocks} />
        <TurnIdChip turnId={turnId} />
      </span>
    </div>
    <div className={`rounded-xl border bg-card px-4 py-3 text-sm leading-relaxed text-txt transition-all duration-500 ${
      failed
        ? 'border-red/40 bg-red/[0.03]'
        : 'border-line'
    } ${streaming && !hasInner ? 'animate-pulse-subtle' : ''}`}>
        {failed && (
          <div className="mb-2 flex items-center gap-1.5 text-[11px] text-red/80">
            <AlertCircle className="h-3 w-3 shrink-0" />
            <span>{tc("stream.interrupted")}</span>
          </div>
        )}
        {hasInner ? (
          <>
            <InnerBlocks
              blocks={blocks}
              streaming={streaming}
              onAnswerQuestion={onAnswerQuestion}
              questionLive={questionLive}
            />
            {/* 尾部等待行：右下角小字+wave dots（2026-11-04 二次反馈：去掉
                外框胶囊，只留与 turn_meta 同语言的轻量文字）。dev-swap 运行
                期间升级为进度态（§4），超 P90 琥珀；发起 turn 已结束也由
                dsTail 持续挂着，直到 run 终态。 */}
            {lineVisible && (
              <div className="mt-1.5 flex justify-end">
                <div className="flex items-center gap-2 px-1 py-0.5">
                <div className="flex items-center gap-0.5">
                  <WaveDot delay={0} color={connLost && !dsTail ? "#eab308" : hex} />
                  <WaveDot delay={150} color={connLost && !dsTail ? "#eab308" : hex} />
                  <WaveDot delay={300} color={connLost && !dsTail ? "#eab308" : hex} />
                </div>
                <span
                  key={tailLabel}
                  className={`animate-fade-in text-xs ${
                    tailAmber ? "text-amber-600 dark:text-amber-400" : "text-muted"
                  }`}
                >
                  {tailLabel}
                </span>
                </div>
              </div>
            )}
          </>
        ) : streaming ? (
          <div className="my-1.5 rounded-md border border-line bg-base/40 px-2.5 py-1.5">
            <div className="flex items-center gap-2">
              {/* Smaller animated wave dots */}
              <div className="flex items-center gap-0.5">
                <WaveDot delay={0} color={hex} />
                <WaveDot delay={150} color={hex} />
                <WaveDot delay={300} color={hex} />
              </div>
              {/* Status text with fade transition */}
              <span key={statusText} className="text-xs text-muted animate-fade-in">
                {statusText}
              </span>
              {/* Subtle progress bar for long waits */}
              {elapsed >= 10 && (
                <div className="ml-auto flex-1 max-w-[80px]">
                  <div className="h-0.5 overflow-hidden rounded-full bg-muted/20">
                    <div
                      className="h-full rounded-full transition-all duration-1000 ease-linear"
                      style={{
                        width: `${Math.min((elapsed - 10) * 3, 100)}%`,
                        background: `linear-gradient(90deg, ${hex}40, ${hex})`
                      }}
                    />
                  </div>
                </div>
              )}
            </div>
          </div>
        ) : (
          // A finished turn with no inner content (e.g. refs-only) must NOT keep
          // pulsing "Thinking…" — that read as a permanently-stuck indicator.
          <span className="text-xs text-faint">{tc("stream.emptyReply")}</span>
        )}
      </div>
      <RefBlocks blocks={blocks} />
    </div>
);
}

export function Dot({ d = 0 }: { d?: number }) {
return (
  <span
    className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted"
    style={{ animationDelay: `${d}ms` }}
  />
);
}

export function WaveDot({ delay, color }: { delay: number; color: string }) {
return (
  <span
    className="h-1.5 w-1.5 rounded-full animate-wave"
    style={{
      backgroundColor: color,
      animationDelay: `${delay}ms`,
      boxShadow: `0 0 6px ${color}60`
    }}
  />
);
}
