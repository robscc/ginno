/** 侧栏品牌行「热更新」按钮（2026-11-05；初版在 SidebarFooter，用户反馈
 *  移到 GinnoWork 品牌行）。settings context.dev_swap_repo 配置后才渲染；
 *  点击 POST /api/devswap/start（无会话发起）→ 进度走 DevSwapCard 徽标，
 *  完成后全局广播确认门；重复点击有后端 409 already_running 兜底。
 *  失败提示（2026-11-05 用户反馈：quiesce 失败只有终端文本看不见）：
 *  start 请求失败与进度事件 fail 态都推 pushNotice——右上角通知中心，
 *  与模型切换等提示同一形态。 */
"use client";

import { useEffect, useState } from "react";
import { Flame, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useDevSwapProgress } from "@/lib/useDevSwapProgress";

export function HotSwapButton() {
  const t = useTranslations("shell");
  const [repo, setRepo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // toggle 态（2026-11-05）：构建中显示「更新中」（pulse），二击 = 取消
  //（kill 构建进程，switch 前取消零风险）。
  const { run } = useDevSwapProgress();
  const running = run?.state === "running";
  // 进度事件驱动的失败（start 被接受但后续 stage fail，如 quiesce 撞上
  // 运行中 turn）也推通知——不只是 start 请求本身的 4xx/5xx。
  const [noticedFail, setNoticedFail] = useState<string | null>(null);
  useEffect(() => {
    if (run?.state === "fail" || run?.state === "rolled_back") {
      const key = `${run.run_id}:${run.state}`;
      if (noticedFail !== key) {
        setNoticedFail(key);
        void import("@/lib/notice").then(({ pushNotice }) => {
          const stage = typeof run.stage === "string" ? run.stage : "";
          pushNotice(
            "error",
            `${t("nav.hotSwapFailed")}${stage ? `（${stage}）` : ""}${
              run.detail ? `：${run.detail}` : ""
            }`,
            { dedupeKey: `devswap-${key}` },
          );
        });
      }
    }
  }, [run?.state, run?.run_id, run?.stage, run?.detail, noticedFail, t]);
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const { getSettings } = await import("@/lib/runtime");
        const s = (await getSettings()) as { context?: { dev_swap_repo?: string } };
        if (alive) setRepo(s.context?.dev_swap_repo || null);
      } catch {
        /* sidecar 未起——按钮不显示 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  if (!repo) return null;
  const act = async (path: string) => {
    setBusy(true);
    try {
      const r = await fetch(path, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!r.ok) {
        const raw = await r.text();
        console.warn("[dev-swap]", path, r.status, raw);
        let detail = raw;
        try {
          const j = JSON.parse(raw);
          detail = String(j.detail ?? j.error ?? raw);
        } catch {
          /* 非 JSON 原样 */
        }
        const { pushNotice } = await import("@/lib/notice");
        pushNotice("error", `${t("nav.hotSwapFailed")}${detail ? `：${detail}` : ""}`, {
          dedupeKey: "devswap-start-fail",
        });
      }
    } catch (e) {
      console.warn("[dev-swap]", path, e);
      const { pushNotice } = await import("@/lib/notice");
      pushNotice("error", `${t("nav.hotSwapFailed")}：${String(e)}`, {
        dedupeKey: "devswap-start-fail",
      });
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() =>
        // 直接取消（2026-11-08 用户反馈「停止不了」）：此前挂 window.confirm
        // 确认——Tauri 的 WKWebView 不弹原生 confirm 对话框，点击静默无
        // 反应。构建阶段取消零风险（switch 前杀构建进程），无需确认门。
        running
          ? void act("/api/devswap/cancel")
          : void act("/api/devswap/start")
      }
      className={`flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium transition-all disabled:opacity-50 ${
        running
          ? // A 案（2026-11-08 用户选定）：实心琥珀 + 黑字，浅色侧栏里对比
            // 最强；hover 转红暗示「点击 = 取消构建」。构建中零风险可取消，
            // 无需确认门（见 onClick 注释）。
            "animate-devswap-in border-amber-500 bg-amber-500 text-zinc-950 hover:border-red-500 hover:bg-red-500 hover:text-white"
          : "border-amber-500/40 bg-amber-500/[0.08] text-amber-600 hover:bg-amber-500/15 dark:text-amber-400"
      }`}
      title={running ? t("nav.hotSwapCancel") : repo}
    >
      {running ? <X className="h-3 w-3 animate-pulse" /> : <Flame className="h-3 w-3" />}
      {running ? t("nav.hotSwapRunning") : t("nav.hotSwap")}
    </button>
  );
}
