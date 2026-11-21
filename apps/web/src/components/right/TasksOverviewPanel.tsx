"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import {
  Activity,
  AlarmClock,
  Bot,
  CalendarClock,
  ExternalLink,
  Flag,
  Hourglass,
  Link2,
  MessageSquare,
  RefreshCw,
  Workflow as WorkflowIcon,
  type LucideIcon,
} from "lucide-react";
import { useGinno } from "@/lib/store";
import { useLocale } from "next-intl";
import { ConfirmModal } from "@/components/ConfirmModal";
import { emptyTasksOverview } from "@/lib/api/tasksOverview";
import {
  formatNextFireAt,
  handleJumpTarget,
  handleKey,
  overviewPhaseGroups,
  overviewTotal,
  refreshTasksOverview,
  stopConfirmVariant,
  stopHandleWithRollback,
  subscribeTasksOverview,
  tasksOverviewSnapshot,
  type TaskHandle,
  type TaskPhase,
  type TasksOverviewState,
} from "@/lib/tasksOverview";

/** 三档分组头（执行中/等你处理/已排期）的图标。 */
const PHASE_ICON: Record<TaskPhase, LucideIcon> = {
  running: Activity,
  waiting: Hourglass,
  scheduled: CalendarClock,
};

/** 六类分组头的图标（lucide，贴暗色主题的既有 3.5–4px 线条密度）——
 * 三档分组后仍作为段内二级组头，保留六类的可识别性。 */
const GROUP_ICON: Record<TaskHandle["kind"], LucideIcon> = {
  turn: MessageSquare,
  schedule: AlarmClock,
  plan: Bot,
  run: WorkflowIcon,
  goal: Flag,
  delegation: Link2,
};

const EMPTY = emptyTasksOverview();

/**
 * 右栏「运行」tab（tasks-overview 方案 §2.2 + 2026-11-21 分类修订）：
 * 按「执行中 / 等你处理 / 已排期」三档分组（phase 服务端真源判定，前端不
 * 猜测），段内保留原六类组头/计数/动作。数据来自 lib/tasksOverview 的模块
 * 级 hub（30s 轮询 + 进 tab 即拉），组件只做渲染与意图派发——分组/计数/
 * 停止回滚逻辑都在 lib 层，node 环境可测。
 *
 * 键盘交互（同日修订）：行不再是可点击 div（按钮嵌在可点击容器里属于嵌套
 * 交互，屏幕阅读器/Tab 遍历都会踩坑）。跳转 affordance 收敛到标题——标题
 * 有跳转目标时渲染为真正的 <button>（可 Tab、Enter 触发），DOM 序在停止
 * 按钮之前；无跳转目标时退化为纯文本，不渲染假按钮。原「查看」按钮退役，
 * 其职能由标题导航控件承担。
 */
export function TasksOverviewPanel() {
  const g = useGinno();
  const router = useRouter();
  const locale = useLocale();
  const t = useTranslations("right.tasksOverview");
  // hub 订阅：面板挂载即拉一次（首个订阅者触发），30s 轮询由 hub 统一管理。
  const [snap, setSnap] = useState<TasksOverviewState | null>(() => tasksOverviewSnapshot());
  const [confirmTarget, setConfirmTarget] = useState<TaskHandle | null>(null);
  const [stoppingIds, setStoppingIds] = useState<Set<string>>(new Set());
  const [stopError, setStopError] = useState<string | null>(null);

  useEffect(() => subscribeTasksOverview(setSnap), []);

  const data = snap?.data ?? EMPTY;
  const phases = useMemo(() => overviewPhaseGroups(data), [data]);
  const total = useMemo(() => overviewTotal(data), [data]);
  // 三态判据（2026-11-21 契约修订）：未加载（loading/从未成功且失败）、
  // 拉取失败但保留旧数据（stale）、确认真空（ok 且零条）——空态🎉只属于
  // 「成功拉到且确实没有活动」，失败绝不能伪装成「没有任务在跑」。
  const loaded = snap?.loaded === true;
  const fetchOk = snap?.ok === true;
  const firstLoading = (snap?.loading ?? false) && !loaded;
  const neverLoaded = !loaded && !firstLoading;
  const showStale = loaded && !fetchOk;
  const showEmpty = loaded && fetchOk && phases.length === 0;

  /** 标题导航控件 / 原查看按钮：跳对应会话（open_session 意图）或 workflow 面板。 */
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
    const key = handleKey(h);
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
      {firstLoading && (
        <div
          className="mx-4 mb-2 rounded-md border border-line bg-card2 px-2.5 py-1.5 text-[11px] text-faint"
          data-testid="tasks-overview-loading"
        >
          {t("loading")}
        </div>
      )}
      {neverLoaded && (
        <div
          className="mx-4 mb-2 flex items-center justify-between gap-2 rounded-md border border-red/40 bg-red/10 px-2.5 py-1.5 text-[11px] text-red"
          data-testid="tasks-overview-load-error"
        >
          <span>{t("firstLoadFailed")}</span>
          <button
            onClick={() => void refreshTasksOverview()}
            className="shrink-0 rounded-md border border-red/40 px-2 py-0.5 text-[11px] transition-colors hover:bg-red/10"
          >
            {t("retry")}
          </button>
        </div>
      )}
      {showStale && (
        <div
          className="mx-4 mb-2 flex items-center justify-between gap-2 rounded-md border border-line bg-card2 px-2.5 py-1.5 text-[11px] text-faint"
          data-testid="tasks-overview-stale"
        >
          <span>{t("loadFailed")}</span>
          <button
            onClick={() => void refreshTasksOverview()}
            className="shrink-0 rounded-md border border-line px-2 py-0.5 text-[11px] text-muted transition-colors hover:bg-card2 hover:text-txt"
          >
            {t("retry")}
          </button>
        </div>
      )}

      <div className="flex-1 space-y-4 px-3 pb-4">
        {showEmpty && (
          <div
            className="px-1 py-12 text-center text-xs text-faint"
            data-testid="tasks-overview-empty"
          >
            <div className="mb-2 text-2xl">🎉</div>
            {t("empty")}
          </div>
        )}
        {phases.map((ph) => {
          const PIc = PHASE_ICON[ph.phase];
          return (
            <section key={ph.phase} data-phase={ph.phase}>
              {/* 三档段头：执行中 / 等你处理 / 已排期 + 段计数 */}
              <div className="mb-1.5 flex items-center gap-1.5 px-1" data-testid={`tasks-overview-phase-${ph.phase}`}>
                <PIc className="h-3.5 w-3.5 text-muted" />
                <span className="text-[11px] font-semibold uppercase tracking-wide text-muted">
                  {t(`phases.${ph.phase}`)}
                </span>
                <span
                  className="rounded-full bg-card2 px-1.5 text-[10px] leading-4 tabular-nums text-muted"
                  data-testid={`tasks-overview-phase-count-${ph.phase}`}
                >
                  {ph.total}
                </span>
              </div>
              {ph.groups.map((grp) => {
                const Ic = GROUP_ICON[grp.kind];
                return (
                  <div key={grp.kind} className="mb-2.5 ml-2.5" data-group={grp.kind}>
                    <div className="mb-1 flex items-center gap-1.5 px-1">
                      <Ic className="h-3 w-3 text-faint" />
                      <span className="text-[11px] font-medium tracking-wide text-faint">
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
                        const stopping = stoppingIds.has(handleKey(h));
                        const stopDisabled = h.stoppable === false;
                        const stopHint = h.stop_hint || t("notStoppable");
                        // schedule 行：下次触发（epoch 秒 → 本地语言/时区）+ 周期
                        // （后端 detail：每天 08:00 / 每周三 …）同时显示。
                        const fireLabel =
                          grp.kind === "schedule" && h.next_fire_at
                            ? t("nextFire", { at: formatNextFireAt(h.next_fire_at, locale) })
                            : null;
                        return (
                          /* 行容器刻意不带 onClick：跳转走标题导航控件、停止走
                             停止按钮——可交互元素不嵌套，Tab 序 = 视觉序。 */
                          <div
                            key={handleKey(h)}
                            className={`flex items-start gap-2.5 rounded-lg border border-line bg-card px-3 py-2.5 ${
                              stopping ? "pointer-events-none opacity-45" : ""
                            }`}
                            data-testid={`tasks-overview-item-${h.kind}`}
                            data-handle-id={h.id}
                            data-phase={h.phase}
                          >
                            <span
                              className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
                              style={{
                                background:
                                  h.phase === "waiting"
                                    ? "var(--st-missed)" // 琥珀：等人（同 workflow human badge 的警示档）
                                    : h.phase === "scheduled"
                                      ? "var(--st-planned)" // 浅蓝：已排期待触发
                                      : "var(--st-running)",
                              }}
                              aria-hidden
                            />
                            <div className="min-w-0 flex-1">
                              {/* 标题 = 导航控件：真正可 Tab / Enter（原生 button），
                                  DOM 序在停止按钮之前；无跳转目标时为纯文本。 */}
                              {jump ? (
                                <button
                                  type="button"
                                  onClick={() => openHandle(h)}
                                  title={h.title}
                                  aria-label={t("openTitle", { title: h.title })}
                                  className="block w-full truncate rounded text-left text-[12.5px] text-txt transition-colors hover:text-blue hover:underline focus-visible:outline focus-visible:outline-1 focus-visible:outline-blue"
                                >
                                  {h.title}
                                </button>
                              ) : (
                                <div className="truncate text-[12.5px] text-txt" title={h.title}>
                                  {h.title}
                                </div>
                              )}
                              <div className="mt-0.5 truncate text-[11px] text-faint">
                                {fireLabel}
                                {fireLabel && h.detail && <span className="mx-1">·</span>}
                                {h.detail}
                                {!jump && (
                                  <span className="ml-1.5 inline-flex items-center gap-0.5">
                                    <ExternalLink className="h-2.5 w-2.5" />
                                    {t("external")}
                                  </span>
                                )}
                              </div>
                            </div>
                            {/* stoppable=false：置灰 + title 显示 stop_hint（方案 §2.1 安全边界）。
                                行容器不带点击层，按钮事件无需拦截冒泡。 */}
                            <button
                              onClick={() => {
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
                          </div>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
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
