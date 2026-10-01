"use client";

// streamCards — ChatStream 的气泡级卡片组件（自 ChatStream.tsx 机械拆出，
// 行为零变化）：turnId chip、子会话顶栏、DSL 提案卡、错误卡、接手分隔线、
// 助手气泡与加载动画点。全部通过 props 取数，不依赖 ChatStream 内部状态。

import { useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronDown, FileEdit, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import { InnerBlocks, RefBlocks, subagentStatusMeta, SubagentKindBadges, type Block } from "@/components/chat/blocks";
import { DiffView } from "@/components/workflow/DiffView";
import type { AgentConfig, SessionMeta } from "@/lib/types";
import type { VersionPropose } from "./streamCore";

async function copyText(t: string) {
try {
  await navigator.clipboard.writeText(t);
  return true;
} catch {
  try {
    const ta = document.createElement("textarea");
    ta.value = t;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
}

/** Click-to-copy per-turn trace UUID. The full id is what you grep the sidecar
 *  logs for (`turn=...`); we show a short prefix to keep the bubble tidy. */
export function TurnIdChip({ turnId }: { turnId?: string }) {
const [copied, setCopied] = useState(false);
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
    title={`turn ${turnId}（点击复制，用于日志定位）`}
    className="rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
  >
    {copied ? "copied" : `#${short}`}
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
const live = g.sessions.find((s) => s.id === session.id) ?? session;
const sub = live.subagent;
if (!sub) return null;
const meta = subagentStatusMeta(sub.status);
const active = sub.status === "running" || sub.status === "waiting";
const back = () => {
  if (live.parent_session_id) g.setActiveSession(live.parent_session_id);
};
return (
  <div className="border-b border-line bg-panel/60 px-6 py-2 text-xs">
    <div className="mx-auto flex max-w-3xl flex-wrap items-center gap-2">
      <span className="shrink-0">🤖</span>
      <span className="min-w-0 max-w-[40%] truncate font-medium text-txt" title={sub.goal}>
        {sub.goal || "子任务"}
      </span>
      <SubagentKindBadges sub={sub} />
      {active && (
        <span
          className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full"
          style={{ background: meta.color }}
        />
      )}
      <span className="shrink-0 text-muted" title={`状态：${meta.label}`}>
        {meta.glyph} {meta.label}
      </span>
      {sub.constraints && (
        <span className="min-w-0 max-w-[24%] truncate text-faint" title={`约束：${sub.constraints}`}>
          约束:{sub.constraints}
        </span>
      )}
      {sub.acceptance && (
        <span className="min-w-0 max-w-[24%] truncate text-faint" title={`验收：${sub.acceptance}`}>
          验收:{sub.acceptance}
        </span>
      )}
      <button
        onClick={back}
        disabled={!live.parent_session_id}
        className="ml-auto shrink-0 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/50 hover:text-violet disabled:opacity-40"
      >
        返回主对话
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
        DSL 变更提案
        <span className="rounded border border-yellow/40 px-1.5 py-0.5 text-[10px] font-normal text-muted">
          {propose.workflow_id} · v{propose.from_version} → 新版本
        </span>
      </div>
      {propose.rationale && (
        <div className="mb-2 text-xs text-muted">理由：{propose.rationale}</div>
      )}
      <button
        onClick={() => setDiffOpen((v) => !v)}
        className="mb-2 flex items-center gap-1 text-[11px] text-faint hover:text-muted"
      >
        <ChevronDown className={`h-3 w-3 transition-transform ${diffOpen ? "" : "-rotate-90"}`} />
        {diffOpen ? "收起 diff" : `查看完整 diff（${hunks} 处改动）`}
      </button>
      {diffOpen && <DiffView diff={propose.diff} />}
      <div className="mt-3 flex gap-2">
        <button
          onClick={() => decide("allow")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
        >
          {busy === "allow" && <Loader2 className="h-3 w-3 animate-spin" />}
          {busy === "allow" ? "应用中…" : "应用变更（创建新版本）"}
        </button>
        <button
          onClick={() => decide("deny")}
          disabled={!!busy}
          className="btn-press flex items-center gap-1 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:bg-red/10 hover:text-red disabled:opacity-50"
        >
          {busy === "deny" && <Loader2 className="h-3 w-3 animate-spin" />}
          拒绝
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
return (
  <div className="rounded-xl border border-red/40 bg-red/10 px-4 py-3">
    <div className="mb-1.5 flex items-center gap-2">
      <AlertCircle className="h-4 w-4 shrink-0 text-red" />
      <span className="text-sm font-medium text-red">请求失败</span>
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
          title="用原输入从头重新发起一次回合"
          className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          从头重试
        </button>
        {onRetryFromCheckpoint && (
          <button
            onClick={onRetryFromCheckpoint}
            disabled={busy}
            title="从最近的检查点继续，保留已完成的工具调用和中间结果"
            className="rounded-lg border border-violet/40 bg-violet/10 px-3 py-1.5 text-xs font-medium text-violet transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            从断点继续
          </button>
        )}
      </div>
    )}
  </div>
);
}

/** C+ 方案②：虚线 pill 分隔线——下一个气泡由哪个 agent 接手。 */
export function HandoffDivider({ agent, agentName }: { agent: AgentConfig | null; agentName?: string }) {
const hex = agentHex(agent?.color);
const name = agent?.name || agentName || "Agent";
return (
  <div className="-my-1 flex items-center gap-2 text-xs">
    <div className="h-px flex-1 border-t border-dashed border-line" />
    <span
      className="inline-flex items-center gap-1.5 rounded-full border border-dashed bg-card px-2.5 py-0.5"
      style={{ borderColor: hex + "55", color: hex }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: hex }} />
      {name} 接手
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
turnId,
failed,
onAnswerQuestion,
questionLive,
}: {
agent: AgentConfig | null;
agentName?: string;
/** 子代理类型名（子会话视图传入）：优先于继承来的 persona 名显示 */
subagentTypeName?: string;
blocks: Block[];
streaming?: boolean;
turnId?: string;
failed?: boolean;
// ask_user resume channel (see InnerBlocks): a parked question card is
// answered from inside the bubble, not from a bottom-docked prompt.
onAnswerQuestion?: (id: string, answer: string, optionIndex: number | null, skip: boolean) => void;
questionLive?: boolean;
}) {
const hex = agentHex(agent?.color);
// 子会话的 persona 是继承父会话的（Dev Agent），标注没有信息量——子代理类型
// 才是它的身份，与侧栏行/子会话顶栏/主对话卡片保持一致（用户反馈 2026-10-01）。
const displayName = subagentTypeName || agent?.name || agentName || "Agent";
const hasInner = blocks.some((b) => b.kind !== "ref");

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
  if (elapsed < 2) return "正在连接模型…";
  if (elapsed < 10) return "模型思考中…";
  return `还在想，可能需要一点时间… (${elapsed}s)`;
}, [elapsed]);
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
      <span className="text-xs text-faint">{streaming ? statusText : "just now"}</span>
      <span className="ml-auto">
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
            <span>回复中断 — 此条回复未完成，可点击下方错误卡片的「从头重试」重新发起</span>
          </div>
        )}
        {hasInner ? (
          <InnerBlocks
            blocks={blocks}
            streaming={streaming}
            onAnswerQuestion={onAnswerQuestion}
            questionLive={questionLive}
          />
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
          <span className="text-xs text-faint">（空回复）</span>
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
