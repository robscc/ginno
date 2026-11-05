/** 侧栏品牌行「热更新」按钮（2026-11-05；初版在 SidebarFooter，用户反馈
 *  移到 GinnoWork 品牌行）。settings context.dev_swap_repo 配置后才渲染；
 *  点击 POST /api/devswap/start（无会话发起）→ 进度走 DevSwapCard 徽标，
 *  完成后全局广播确认门；重复点击有后端 409 already_running 兜底。 */
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
      if (!r.ok) console.warn("[dev-swap]", path, r.status, await r.text());
    } catch (e) {
      console.warn("[dev-swap]", path, e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      disabled={busy}
      onClick={() =>
        running
          ? void (window.confirm(t("nav.hotSwapCancelConfirm")) && act("/api/devswap/cancel"))
          : void act("/api/devswap/start")
      }
      className={`flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] transition-all disabled:opacity-50 ${
        running
          ? "animate-devswap-in border-amber-500/60 bg-amber-500/20 text-amber-600 hover:bg-red-500/20 dark:text-amber-400"
          : "border-amber-500/40 bg-amber-500/[0.08] text-amber-600 hover:bg-amber-500/15 dark:text-amber-400"
      }`}
      title={running ? t("nav.hotSwapCancel") : repo}
    >
      {running ? <X className="h-3 w-3 animate-pulse" /> : <Flame className="h-3 w-3" />}
      {running ? t("nav.hotSwapRunning") : t("nav.hotSwap")}
    </button>
  );
}
