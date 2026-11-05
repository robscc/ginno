"use client";

// DevSwapCard — dev-swap 热切换进度时间线卡片（docs/dev-swap-progress-design.md
// §1/§5）。挂在侧栏品牌行 GinnoWork 右侧：内联 pill（状态点 + 阶段 + pct），
// hover 弹出六阶段纵向时间线（✔/◐/○/✖ 状态点 + 阶段名 + 明细 + 耗时），失败
// 红色停在失败步并可展开尾 30 行日志；全部完成转绿 1.5s 后自动收起（徽标
// dev·{指纹} 即终态凭证）。

import { useEffect, useMemo, useState } from "react";
import { subscribeDevSwapConfirm, setDevSwapPendingConfirm, isDevSwapConfirmPending } from "@/lib/devswap/bus";
import { useTranslations } from "next-intl";
import { useDevSwapProgress } from "@/lib/useDevSwapProgress";
import { resyncDevSwapRun } from "@/lib/devswap/bus";
import {
  DEVSWAP_STAGES,
  stageCells,
  type DevSwapRun,
  type DevSwapStage,
} from "@/lib/devswap/types";
import { expectationFor, fmtClock, progressBar } from "@/lib/devswap/stats";

/** chat 域同款动态 key 收敛转型（理由见 streamCards.tsx useChatT）。 */
function useDevswapT() {
  const tc = useTranslations("devswap");
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  return { tc, tr };
}

/** 状态点字符：✔ 完成 / ◐ 进行 / ○ 待定 / ✖ 失败。 */
function dot(cell: "done" | "active" | "fail" | "pending"): string {
  return cell === "done" ? "✔" : cell === "active" ? "◐" : cell === "fail" ? "✖" : "○";
}

function dotColor(cell: "done" | "active" | "fail" | "pending"): string {
  switch (cell) {
    case "done":
      return "text-emerald-600 dark:text-emerald-400";
    case "active":
      return "text-amber-600 dark:text-amber-400";
    case "fail":
      return "text-red-600 dark:text-red-400";
    default:
      return "text-faint";
  }
}

export function DevSwapCard() {
  const { run } = useDevSwapProgress();
  const { tr } = useDevswapT();
  // 全页唯一的快照兜底（reload 后 bus 空）：卡片挂载拉一次。气泡层的
  // useDevSwapProgress 只订阅不拉，防 GET 风暴（见该 hook 注释）。
  useEffect(() => {
    void resyncDevSwapRun();
  }, []);
  // ok → 绿 1.5s 后收起（一次性，直到下一个 run）。
  const [gone, setGone] = useState(false);
  const [logOpen, setLogOpen] = useState(false);
  // 交互形态（2026-11-04 二次反馈）：改为内联小徽标 + hover 弹出（见 return），
  // 无需折叠/关闭状态。
  const [confirmPending, setConfirmPending] = useState(isDevSwapConfirmPending());
  const [swapping, setSwapping] = useState(false);
  useEffect(() => subscribeDevSwapConfirm(setConfirmPending), []);
  const doSwap = async () => {
    setSwapping(true);
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const res = await invoke<{ ok: boolean; rolled_back?: boolean; detail?: string }>(
        "finish_dev_swap",
      );
      console.info("[dev-swap] finished:", res);
    } catch (e) {
      console.warn("[dev-swap] finish_dev_swap unavailable:", e);
    } finally {
      setSwapping(false);
      setDevSwapPendingConfirm(false);
    }
  };
  // 当前阶段已耗时（秒）：卡片自身重渲染太便宜，1s 一跳即可。
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!run || run.state !== "running") return;
    const timer = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [run]);

  useEffect(() => {
    if (!run || run.state !== "ok") {
      setGone(false);
      return;
    }
    const timer = setTimeout(() => setGone(true), 1500);
    return () => clearTimeout(timer);
  }, [run]);

  const cells = useMemo(() => (run ? stageCells(run) : null), [run]);

  if (!run || !cells || gone) return null;

  const failed = run.state === "fail" || run.state === "rolled_back";
  const accent = failed
    ? "border-red/50"
    : run.state === "ok"
      ? "border-emerald-500/50"
      : "border-amber-500/50";

  const stageElapsed = Math.max(0, Math.floor(Date.now() / 1000 - (run.ts ?? Math.floor(Date.now() / 1000))));
  const exp = expectationFor(run.stage);
  const slow = exp.hasP90 && run.state === "running" && stageElapsed > exp.p90;
  const logLines = (run.log_tail ?? "").split("\n").filter(Boolean);

  // 交互形态（2026-11-04 二次反馈）：不再 fixed 浮卡（挡内容「动都动不了」），
  // 改为 TopBar 内联小徽标 + hover 弹出完整时间线。徽标本身只占一粒 pill
  // 宽度，pointer-events 天然只落在徽标上；卡片仅 hover 可见，可交互（日志
  // 展开等），离开即收起。
  const compactPct = typeof run.pct === "number" ? `${Math.round(run.pct)}%` : "";
  return (
    <div className="relative flex items-center gap-2" data-testid="devswap-Card-root">
      {/* 切换确认门（2026-11-05）：构建成功后常驻琥珀条，确认才重启切换。
          不随 hover 收起——这是需要用户决断的状态。 */}
      {confirmPending && (
        <div className="flex items-center gap-2 rounded-full border border-amber-500/60 bg-amber-500/10 px-2.5 py-0.5 text-[11px] text-amber-600 dark:text-amber-400">
          <span>{tr("card.confirmHint")}</span>
          <button
            type="button"
            disabled={swapping}
            onClick={doSwap}
            className="rounded-full bg-amber-500 px-2 py-px text-[11px] font-medium text-white hover:bg-amber-600 disabled:opacity-50"
          >
            {swapping ? tr("card.swapping") : tr("card.confirmSwap")}
          </button>
          <button
            type="button"
            disabled={swapping}
            onClick={() => setDevSwapPendingConfirm(false)}
            className="text-faint hover:text-txt disabled:opacity-50"
          >
            {tr("card.postpone")}
          </button>
        </div>
      )}
      <div className="group/devswap relative flex items-center" data-testid="devswap-Card-root">
      <button
        type="button"
        className={`flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${accent} ${
          run.state === "running" ? "bg-amber-500/10 text-amber-600 dark:text-amber-400" : "bg-card text-muted"
        }`}
        title={tr("card.hoverHint")}
      >
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${run.state === "running" ? "animate-pulse bg-amber-500" : failed ? "bg-red-500" : "bg-emerald-500"}`} aria-hidden />
        <span className="font-medium">{tr(`card.stage.${run.stage}`)}</span>
        {compactPct && <span className="font-mono text-faint">{compactPct}</span>}
        {slow && <span className="font-mono text-faint">· {tr("card.slowShort")}</span>}
      </button>

      {/* hover 弹出：默认不占布局、不可交互；组内 hover 才显示。
          left-0（从 pill 向右弹出）：挂在侧栏品牌行（2026-11-04 迁自 TopBar），
          弹层向右覆盖聊天区——aside 无 overflow-hidden 不会被裁。 */}
      <div
        className={`pointer-events-none absolute left-0 top-full z-50 mt-1 hidden w-80 rounded-xl border ${accent} bg-card text-sm opacity-0 shadow-xl transition-opacity duration-150 group-hover/devswap:pointer-events-auto group-hover/devswap:block group-hover/devswap:opacity-100`}
        data-testid="devswap-card"
      >
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <span className="font-medium text-txt">
            {tr("card.title")} <span className="font-mono text-[10px] text-faint">{run.run_id}</span>
          </span>
          {slow && (
            <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-600 dark:text-amber-400">
              {tr("card.slow", { time: fmtClock(exp.p90) })}
            </span>
          )}
        </div>

        <div className="px-3 py-2">
          {DEVSWAP_STAGES.map((stage: DevSwapStage, i: number) => {
            const cell = cells[i];
            const isCurrent = cell === "active" || cell === "fail";
            return (
              <div key={stage} className="flex items-start gap-2 py-0.5">
                <span className={`w-4 shrink-0 text-center font-mono ${dotColor(cell)}`}>
                  {dot(cell)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span
                      className={
                        cell === "pending"
                          ? "text-faint"
                          : isCurrent
                            ? "font-medium text-txt"
                            : "text-muted"
                      }
                    >
                      {tr(`card.stage.${stage}`)}
                    </span>
                    {isCurrent && run.state === "running" && (
                      <span className="ml-auto font-mono text-[10px] text-faint">
                        {fmtClock(stageElapsed)}
                        {exp.ema - stageElapsed > 1 ? ` / ${tr("card.eta", { time: fmtClock(exp.ema - stageElapsed) })}` : ""}
                      </span>
                    )}
                  </div>
                  {isCurrent && (
                    <div className="mt-0.5 text-[11px] leading-snug text-muted">
                      {run.detail || tr("card.working")}
                      {typeof run.pct === "number" && (
                        <span className="ml-1 font-mono">
                          {progressBar(run.pct)} {Math.round(run.pct)}%
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}

          {run.state === "rolled_back" && (
            <div className="mt-2 rounded-md border border-red/40 bg-red/[0.06] px-2 py-1.5 text-[11px] text-red-700 dark:text-red-300">
              {tr("card.rolledBack")}
            </div>
          )}
          {run.state === "ok" && (
            <div className="mt-2 text-[11px] text-emerald-600 dark:text-emerald-400">
              {tr("card.finished")}
            </div>
          )}

          {failed && !!logLines.length && (
            <div className="mt-2">
              <button
                onClick={() => setLogOpen((o) => !o)}
                className="text-[11px] text-muted underline decoration-line2 underline-offset-2 hover:text-txt"
              >
                {logOpen ? tr("card.hideLogs") : tr("card.logs", { n: Math.min(logLines.length, 30) })}
              </button>
              {logOpen && (
                <pre className="mt-1 max-h-40 overflow-auto rounded-md border border-line bg-base/60 p-2 font-mono text-[10px] leading-snug text-muted">
                  {logLines.slice(-30).join("\n")}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
      </div>
    </div>
  );
}
