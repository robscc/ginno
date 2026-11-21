"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import {
  AlarmClock,
  Bot,
  ExternalLink,
  Flag,
  Link2,
  MessageSquare,
  RefreshCw,
  Workflow as WorkflowIcon,
  type LucideIcon,
} from "lucide-react";
import { useGinno } from "@/lib/store";
import { ConfirmModal } from "@/components/ConfirmModal";
import { emptyTasksOverview } from "@/lib/api/tasksOverview";
import {
  handleJumpTarget,
  overviewGroups,
  overviewTotal,
  refreshTasksOverview,
  stopConfirmVariant,
  stopHandleWithRollback,
  subscribeTasksOverview,
  tasksOverviewSnapshot,
  type TaskHandle,
  type TaskHandleKind,
  type TasksOverviewState,
} from "@/lib/tasksOverview";

/** 六组分组头的图标（lucide，贴暗色主题的既有 3.5–4px 线条密度）。 */
const GROUP_ICON: Record<TaskHandleKind, LucideIcon> = {
  turn: MessageSquare,
  schedule: AlarmClock,
  plan: Bot,
  run: WorkflowIcon,
  goal: Flag,
  delegation: Link2,
};

const EMPTY = emptyTasksOverview();

/**
 * 右栏「运行」tab（tasks-overview 方案 §2.2）：六类活动句柄的分组总览 +
 * 单项停止。数据来自 lib/tasksOverview 的模块级 hub（30s 轮询 + 进 tab 即拉），
 * 组件只做渲染与意图派发——分组/计数/停止回滚逻辑都在 lib 层，node 环境可测。
 */
export function TasksOverviewPanel() {
  const g = useGinno();
  const router = useRouter();
  const t = useTranslations("right.tasksOverview");
  // hub 订阅：面板挂载即拉一次（首个订阅者触发），30s 轮询由 hub 统一管理。
  const [snap, setSnap] = useState<TasksOverviewState | null>(() => tasksOverviewSnapshot());
  const [confirmTarget, setConfirmTarget] = useState<TaskHandle | null>(null);
  const [stoppingIds, setStoppingIds] = useState<Set<string>>(new Set());
  const [stopError, setStopError] = useState<string | null>(null);

  useEffect(() => subscribeTasksOverview(setSnap), []);

  const data = snap?.data ?? EMPTY;
  const groups = useMemo(() => overviewGroups(data), [data]);
  const total = useMemo(() => overviewTotal(data), [data]);

  /** 行点击 / 查看按钮：跳对应会话（open_session 意图）或 workflow 面板。 */
  const openHandle = (h: TaskHandle) => {
    const target = handleJumpTarget(h);
    if (!target) return;
    if (target.type === "workflow") {
      // 与 AppShell 的 __ginnoOpenWorkflowRun 同语义：回工作区 + 开 workflow tab。
      if (window.location.pathname !== "/") router.push("/");
      g.setRightTab("workflow");
      g.setRightPanelOpen(true);
      return;
    }
    if (target.type === "schedule") {
      if (window.location.pathname !== "/scheduled") router.push("/scheduled");
      return;
    }
    // open_session 意图：setActiveSession + 回工作区 + focus-latest（AppShell
    // 通知点击/deep link 同款三步，ChatStream 据此吸底滚动）。
    g.setActiveSession(target.sid);
    if (window.location.pathname !== "/") router.push("/");
    window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: target.sid }));
  };

  /** 确认后的停止：乐观移除（hub 内完成）→ 失败回滚重拉 + 错误条。 */
  const doStop = (h: TaskHandle) => {
    const key = `${h.kind}:${h.id}`;
    setStoppingIds((prev) => new Set(prev).add(key));
    setStopError(null);
    stopHandleWithRollback(h)
      .catch(() => setStopError(t("stopFailed", { title: h.title })))
      .finally(() => {
        setStoppingIds((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
      });
  };

  return (
    <div className="flex h-full flex-col" data-testid="tasks-overview-panel">
      <div className="flex items-center px-4 pb-2 pt-4">
        <span className="text-sm font-semibold text-txt">{t("title")}</span>
        {total > 0 && (
          <span
            className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] tabular-nums text-muted"
            data-testid="tasks-overview-total"
          >
            {total}
          </span>
        )}
        <button
          onClick={() => void refreshTasksOverview()}
          title={t("refresh")}
          aria-label={t("refresh")}
          className="ml-auto flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted transition-colors hover:bg-card2 hover:text-txt"
        >
          <RefreshCw className="h-3 w-3" /> {t("refresh")}
        </button>
      </div>

      {stopError && (
        <div
          className="mx-4 mb-2 rounded-md border border-red/40 bg-red/10 px-2.5 py-1.5 text-[11px] text-red"
          data-testid="tasks-overview-stop-error"
        >
          {stopError}
        </div>
      )}
      {!snap?.ok && snap?.loaded && (
        <div
          className="mx-4 mb-2 rounded-md border border-line bg-card2 px-2.5 py-1.5 text-[11px] text-faint"
          data-testid="tasks-overview-stale"
        >
          {t("loadFailed")}
        </div>
      )}

      <div className="flex-1 space-y-4 px-3 pb-4">
        {groups.length === 0 && (
          <div
            className="px-1 py-12 text-center text-xs text-faint"
            data-testid="tasks-overview-empty"
          >
            <div className="mb-2 text-2xl">🎉</div>
            {t("empty")}
          </div>
        )}
        {groups.map((grp) => {
          const Ic = GROUP_ICON[grp.kind];
          return (
            <section key={grp.kind} data-group={grp.kind}>
              <div className="mb-1.5 flex items-center gap-1.5 px-1">
                <Ic className="h-3.5 w-3.5 text-muted" />
                <span className="text-[11px] font-medium tracking-wide text-muted">
                  {t(`groups.${grp.kind}`)}
                </span>
                <span
                  className="rounded-full bg-card2 px-1.5 text-[10px] leading-4 tabular-nums text-muted"
                  data-testid={`tasks-overview-count-${grp.kind}`}
                >
                  {grp.items.length}
                </span>
              </div>
              <div className="space-y-1.5">
                {grp.items.map((h) => {
                  const jump = handleJumpTarget(h);
                  const stopping = stoppingIds.has(`${h.kind}:${h.id}`);
                  const stopDisabled = h.stoppable === false;
                  const stopHint = h.stop_hint || t("notStoppable");
                  return (
                    <div
                      key={h.id}
                      className={`flex items-start gap-2.5 rounded-lg border border-line bg-card px-3 py-2.5 transition-colors ${
                        jump ? "cursor-pointer hover:border-line2 hover:bg-card2" : ""
                      } ${stopping ? "pointer-events-none opacity-45" : ""}`}
                      onClick={() => openHandle(h)}
                      data-testid={`tasks-overview-item-${h.kind}`}
                      data-handle-id={h.id}
                    >
                      <span
                        className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
                        style={{ background: "var(--st-running)" }}
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-[12.5px] text-txt" title={h.title}>
                          {h.title}
                        </div>
                        <div className="mt-0.5 truncate text-[11px] text-faint">
                          {h.detail ??
                            (h.next_fire_at ? t("nextFire", { at: h.next_fire_at }) : "")}
                          {!jump && (
                            <span className="ml-1.5 inline-flex items-center gap-0.5">
                              <ExternalLink className="h-2.5 w-2.5" />
                              {t("external")}
                            </span>
                          )}
                        </div>
                      </div>
                      {/* stoppable=false：置灰 + title 显示 stop_hint（方案 §2.1 安全边界） */}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          if (!stopDisabled) setConfirmTarget(h);
                        }}
                        disabled={stopDisabled}
                        title={stopDisabled ? stopHint : t(`stopBtn.${grp.kind}`)}
                        className={`shrink-0 rounded-md border px-2 py-1 text-[11px] transition-colors ${
                          stopDisabled
                            ? "cursor-not-allowed border-line text-faint"
                            : "border-red/40 text-red hover:border-red/70 hover:bg-red/10"
                        }`}
                        data-testid={`tasks-overview-stop-${h.kind}`}
                      >
                        {t(`stopBtn.${grp.kind}`)}
                      </button>
                      {/* 查看：不可停但有跳转目标的句柄，查看是主 affordance（原型语义） */}
                      {stopDisabled && jump && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            openHandle(h);
                          }}
                          className="shrink-0 rounded-md px-1.5 py-1 text-[11px] text-blue hover:underline"
                        >
                          {t("view")}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>

      {/* 停止薄确认：schedule=暂停可恢复，其余=不可撤销（文案分支见方案 §2.2） */}
      {confirmTarget && (
        <ConfirmModal
          title={t(`confirmTitle.${confirmTarget.kind}`)}
          message={
            stopConfirmVariant(confirmTarget.kind) === "pause"
              ? t("confirmBodyPause", { title: confirmTarget.title })
              : t("confirmBodyIrreversible", { title: confirmTarget.title })
          }
          confirmLabel={t(`stopBtn.${confirmTarget.kind}`)}
          onConfirm={() => {
            const h = confirmTarget;
            setConfirmTarget(null);
            doStop(h);
          }}
          onCancel={() => setConfirmTarget(null)}
        />
      )}
    </div>
  );
}
