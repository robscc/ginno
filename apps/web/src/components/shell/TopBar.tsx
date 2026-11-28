"use client";

import { useState } from "react";
import { ArrowDownUp } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { AgentConfig, ContextState, SessionMeta, SessionUsage } from "@/lib/types";
import { agentHex } from "@/lib/theme";
import { fmtTokens } from "@/lib/format";
import { AgentIcon } from "@/components/icons";
import { GoalChip } from "./GoalChip";
import { RemotePeerBadge } from "./RemotePeerBadge";
import { ContextFoldersChip } from "./ContextFoldersChip";
import { LinkedSessionsChip } from "./LinkedSessionsChip";
import { TopBarFuncButton } from "./TopBarFuncButton";
import { SessionActivityIndicator } from "./SessionActivityIndicator";



/** Click-to-copy session uuid chip — same shape as the per-turn trace chip on
 * chat bubbles. The full id is what you grep the sidecar logs for
 * (`session=...`); we show a short prefix to keep the bar tidy. */
function SessionIdChip({ sessionId }: { sessionId?: string }) {
  const tr = useTranslations("shell");
  const [copied, setCopied] = useState(false);
  if (!sessionId) return null;
  const short = sessionId.slice(0, 8);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(sessionId);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title={tr("topbar.idChipTitle", { id: sessionId })}
      className="shrink-0 whitespace-nowrap rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
    >
      {copied ? tr("topbar.copied") : `#${short}`}
    </button>
  );
}

/** Context-window occupancy ring (上下文占用圆环)。SVG 圆环 + 中心百分比：
 * 占用 = 最近一次 LLM 调用的全量 prompt / 会话模型的上下文窗口。
 * 变色分档（2026-11-16 预警口径统一）：优先按 compact_pct（占用 ÷ 自动
 * 压缩阈值）—— ≥85% 琥珀、≥95% 红（阈值点即 100%）；无 threshold 数据
 * 的旧 runtime 回退窗口百分比（70%/90%）。环上以刻度线标出压缩触发点，
 * tooltip 给出精确 token 数与距阈值余量。 */
function ContextRing({ ctx }: { ctx: ContextState | null }) {
  const tr = useTranslations("shell");
  if (!ctx || !ctx.window) return null;
  const pct = Math.min(100, Math.max(0, ctx.percent));
  // stroke 几何：r=8, 周长 2πr≈50.27；dashOffset 按 pct 挖空。
  const R = 8;
  const C = 2 * Math.PI * R;
  const hasThreshold =
    typeof ctx.compact_pct === "number" && typeof ctx.threshold === "number" && ctx.threshold > 0;
  const cpct = hasThreshold ? Math.min(999, Math.max(0, ctx.compact_pct!)) : null;
  // 分档：阈值口径优先（85/95），旧数据回退窗口口径（70/90）。
  const warnLevel = hasThreshold
    ? cpct! >= 95
      ? "red"
      : cpct! >= 85
        ? "amber"
        : "ok"
    : pct >= 90
      ? "red"
      : pct >= 70
        ? "amber"
        : "ok";
  const color =
    warnLevel === "red"
      ? "var(--st-error, #ef4444)"
      : warnLevel === "amber"
        ? "#d9a93e"
        : "#8b5cf6";
  // 淡底（2026-11-16 用户反馈）：同色系 RGB 的低透明字面量——CSS 变量
  // 没法直接做 alpha 分解，三档各写两份（实心底 8% / track 22%）。底色
  // 跟随语义档变色，占用越高底色越「暖」，不引入第四种颜色语言。
  const rgb =
    warnLevel === "red" ? "239,68,68" : warnLevel === "amber" ? "217,169,62" : "139,92,246";
  const trackColor = `rgba(${rgb},0.22)`;
  // 压缩触发刻度：threshold 在环上对应的圆弧位置（12 点起顺时针）。阈值
  // ≥ 窗口时刻度退化为整圈，不画（异常配置，无参考意义）。
  const thresholdPctOnRing = hasThreshold ? Math.min(100, (ctx.threshold! / ctx.window) * 100) : 0;
  const showTick = hasThreshold && thresholdPctOnRing > 4 && thresholdPctOnRing < 96;
  const tickAngle = (thresholdPctOnRing / 100) * 2 * Math.PI - Math.PI / 2;
  const tickX1 = 10 + (R - 2.6) * Math.cos(tickAngle);
  const tickY1 = 10 + (R - 2.6) * Math.sin(tickAngle);
  const tickX2 = 10 + (R + 2.6) * Math.cos(tickAngle);
  const tickY2 = 10 + (R + 2.6) * Math.sin(tickAngle);
  const suggest = warnLevel !== "ok";
  const title = hasThreshold
    ? tr("topbar.contextTitleThreshold", {
        used: fmtTokens(ctx.tokens),
        window: fmtTokens(ctx.window),
        percent: ctx.percent.toFixed(0),
        threshold: fmtTokens(ctx.threshold!),
        compactPct: ctx.compact_pct!.toFixed(0),
      })
    : tr("topbar.contextTitle", {
        used: fmtTokens(ctx.tokens),
        window: fmtTokens(ctx.window),
        percent: ctx.percent.toFixed(0),
      });
  return (
    <span
      className="flex h-8 w-8 shrink-0 items-center justify-center"
      title={title + (suggest ? " — " + tr("topbar.contextSuggestCompact") : "")}
    >
      <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden>
        {/* 淡底：实心圆 8% 同色系——弧线浮在柔光底上（用户反馈 2026-11-16
            「最好能有个淡淡的底色」），track 同步从硬灰线换成 22% 同色。 */}
        <circle cx="10" cy="10" r={R} fill={`rgba(${rgb},0.08)`} />
        <circle cx="10" cy="10" r={R} fill="none" stroke={trackColor} strokeWidth="2.5" />
        <circle
          cx="10"
          cy="10"
          r={R}
          fill="none"
          stroke={color}
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeDasharray={C}
          strokeDashoffset={C * (1 - pct / 100)}
          transform="rotate(-90 10 10)"
        />
        {showTick && (
          <line
            x1={tickX1}
            y1={tickY1}
            x2={tickX2}
            y2={tickY2}
            stroke="currentColor"
            strokeWidth="1"
            opacity="0.55"
          />
        )}
        <text
          x="10"
          y="10"
          textAnchor="middle"
          dominantBaseline="central"
          fontSize="7"
          fill="currentColor"
          className="text-muted"
        >
          {ctx.percent >= 100 ? "!" : Math.round(pct)}
        </text>
      </svg>
    </span>
  );
}

/** Dev 指纹徽标 + dev-swap 进度 hover 卡已迁往侧栏品牌行
 *  （components/devswap/DevBuildBadge / DevSwapCard，挂 SidebarHeader）。 */

export function TopBar({
  session,
  agent,
  running,
  waiting,
  compacting,
  usage,
  context,
}: {
  session: SessionMeta | null;
  agent: AgentConfig | null;
  running: boolean;
  waiting?: boolean;
  compacting?: boolean;
  usage?: SessionUsage | null;
  context?: ContextState | null;
}) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const [usageOpen, setUsageOpen] = useState(false);
  const hex = agentHex(agent?.color);



  return (
    <header className="flex min-h-14 min-w-0 shrink-0 select-none flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line px-5 py-1.5">
      <div className="flex min-w-[min(100%,12rem)] flex-1 flex-wrap items-center gap-x-2.5 gap-y-1">
        {/* 身份区(topbar-unification.md §2.4):agent 纯图标、标题左侧,名称进
            tooltip——侧栏行同款「图标+颜色」识别,让出的宽度全给标题。 */}
        {agent && (
          <span
            className="flex h-[18px] w-[18px] shrink-0 items-center justify-center"
            style={{ color: hex }}
            title={agent.name}
          >
            <AgentIcon name={agent.icon} className="h-[18px] w-[18px]" />
          </span>
        )}
        <h1 className="min-w-[5rem] max-w-full truncate text-[15px] font-semibold tracking-tight text-txt" title={session?.title || tr("topbar.newSession")}>
          {session?.title || tr("topbar.newSession")}
        </h1>
        <SessionIdChip sessionId={session?.id} />
        {/* 远端归属徽标（remote-session-design.md R3）：会话挂 remote_peer
            时紧跟标题区，一眼看出执行方在另一台机器。 */}
        {session?.remote_peer && (
          <RemotePeerBadge
            slug={session.remote_peer}
            resolvedName={session.remote_peer_name}
          />
        )}
      </div>

      {/* 功能按钮组(§2.3):五个同宽 32px,徽标 + hover 数据 + click 面板;
          home(无 session)整组隐藏(维持现状)。 */}
      {session && (
        <div className="flex shrink-0 items-center gap-0.5">
          <span className="topbar-session-activity flex w-8 shrink-0 items-center justify-center">
            <SessionActivityIndicator activity={waiting ? "waiting" : compacting ? "compacting" : running ? "running" : "idle"} />
          </span>

          <GoalChip sessionId={session.id} />

          <ContextFoldersChip session={session} />

          <LinkedSessionsChip session={session} />

          {/* ⭕ 上下文占用圆环：最近一次调用的 prompt / 模型窗口。 */}
          <ContextRing ctx={context ?? null} />

          {/* 📊 Stats:纯 hover 数据(决策点 D,会话累计口径)。数字走紧凑
              k/M/B（lib/format）——原始值动辄数十万 token，全数字在
              tooltip 里反而难读。 */}
          {usage && usage.calls > 0 && (
            <div className="relative">
              <TopBarFuncButton
                title={tr("topbar.usageTitle", {
                  input: fmtTokens(usage.input_tokens),
                  cache: fmtTokens(usage.cache_read_tokens),
                  output: fmtTokens(usage.output_tokens),
                  calls: usage.calls,
                })}
                onClick={() => setUsageOpen((v) => !v)}
                open={usageOpen}
              >
                <ArrowDownUp className="h-4 w-4" />
              </TopBarFuncButton>
              {usageOpen && (
                // 点击展开的分色明细卡（2026-11-11）：与 TurnMetaLine 胶囊
                // 同一视觉语言（↑蓝/↓绿/中性），缓存命中率只在这可见。
                <>
                  <div className="fixed inset-0 z-40" onClick={() => setUsageOpen(false)} />
                  <div className="absolute right-0 top-9 z-50 w-64 rounded-lg border border-line bg-card p-2.5 font-mono text-[11px] shadow-lg">
                    {(() => {
                      const hit = usage.input_tokens > 0
                        ? Math.round((usage.cache_read_tokens / usage.input_tokens) * 100)
                        : 0;
                      const row = "flex items-center justify-between py-0.5";
                      return (
                        <>
                          <div className="mb-1 text-[10px] uppercase tracking-wide text-faint">
                            {tr("topbar.usageHeading")}
                          </div>
                          <div className={row}>
                            <span className="text-sky-600 dark:text-sky-400">{tr("topbar.usageIn")}</span>
                            <span>{usage.input_tokens.toLocaleString()}</span>
                          </div>
                          <div className={row}>
                            <span className="text-sky-500/60">{tr("topbar.usageCache")}</span>
                            <span>
                              {usage.cache_read_tokens.toLocaleString()} · {hit}%
                            </span>
                          </div>
                          <div className={row}>
                            <span className="text-emerald-600 dark:text-emerald-400">{tr("topbar.usageOut")}</span>
                            <span>{usage.output_tokens.toLocaleString()}</span>
                          </div>
                          <div className={`${row} border-t border-line2/50 mt-1 pt-1`}>
                            <span className="text-muted">{tr("topbar.usageCalls")}</span>
                            <span>{usage.calls.toLocaleString()}</span>
                          </div>
                        </>
                      );
                    })()}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      <div className="ml-auto flex shrink-0 items-center gap-2">

      </div>
    </header>
  );
}
