"use client";

// PlanBoard — 任务清单卡（subagent-plan-orchestration-design.md §1 前端
// plan board / §8 P3）。数据 = store.plansBySession（打开会话 REST 拉取 +
// plan.updated WS 事件实时刷，watcher 每次落盘推整张快照）。
//
// 渲染：清单级折叠 + 进度计数 + 逐条状态（running/done/failed/stopped 与
// 子代理五态同词汇同 glyph；pending/blocked 本地补齐）+ 依赖/后端/类型
// 徽标；有条目子会话时点行跳过去。写面只保留用户侧动作：pending_confirm →
// 确认/取消、active → 取消（增删改/重试是主 agent 的 amend_plan 工具面，
// UI 不重复）。终态清单默认折叠，收在底部不抢注意力。

import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight, ClipboardList, Loader2 } from "lucide-react";
import { useGinno } from "@/lib/store";
import { cancelSessionPlan, confirmSessionPlan } from "@/lib/runtime";
import type { Plan, PlanItem, PlanItemStatus } from "@/lib/types";
import { SUBAGENT_STATUS_META } from "@/components/chat/blocks/types";
import { DuckStatusIcon } from "@/components/chat/blocks/duckStatus";

/** 条目五态的展示元数据：running 及终态复用子代理词汇（设计 §2「前端零
 * 新词汇」），pending/blocked 是清单特有态、本地补齐。 */
const ITEM_META: Record<PlanItemStatus, { glyph: string; color: string }> = {
  pending: { glyph: "•", color: "#a1a1aa" },
  blocked: { glyph: "⊘", color: "#a1a1aa" },
  running: { glyph: SUBAGENT_STATUS_META.running.glyph, color: SUBAGENT_STATUS_META.running.color },
  done: { glyph: SUBAGENT_STATUS_META.done.glyph, color: SUBAGENT_STATUS_META.done.color },
  failed: { glyph: SUBAGENT_STATUS_META.failed.glyph, color: SUBAGENT_STATUS_META.failed.color },
  stopped: { glyph: SUBAGENT_STATUS_META.stopped.glyph, color: SUBAGENT_STATUS_META.stopped.color },
};

// as const：值是字面量类型，next-intl 的严格消息键检查才接受（动态拼串不行）。
const PLAN_STATUS_KEY = {
  pending_confirm: "planBoard.statusPendingConfirm",
  active: "planBoard.statusActive",
  completed: "planBoard.statusCompleted",
  cancelled: "planBoard.statusCancelled",
} as const;

export function PlanBoard({ sessionId }: { sessionId: string }) {
  const g = useGinno();
  const t = useTranslations("chat");
  // 折叠覆盖以 plan_id 记忆；未记录过的清单按状态取默认（open 展开 / 终态折叠）。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<Record<string, "confirm" | "cancel" | undefined>>({});

  const plans = useMemo(
    () =>
      [...(g.plansBySession[sessionId] ?? [])].sort((a, b) => (b.updated ?? b.created ?? 0) - (a.updated ?? a.created ?? 0)),
    [g.plansBySession, sessionId],
  );
  if (plans.length === 0) return null;

  const act = async (plan: Plan, action: "confirm" | "cancel") => {
    if (busy[plan.plan_id]) return;
    setBusy((b) => ({ ...b, [plan.plan_id]: action }));
    try {
      const r =
        action === "confirm"
          ? await confirmSessionPlan(sessionId, plan.plan_id)
          : await cancelSessionPlan(sessionId, plan.plan_id);
      if (r?.plan) g.notifyPlanUpdated(r.plan);
      else await g.loadPlans(sessionId);
    } catch {
      /* sidecar down：保持原状，plan.updated 会追平 */
    } finally {
      setBusy((b) => ({ ...b, [plan.plan_id]: undefined }));
    }
  };

  return (
    <div className="mx-auto mb-2 flex w-full max-w-3xl flex-col gap-2">
      {plans.map((plan) => {
        const terminal = plan.status === "completed" || plan.status === "cancelled";
        const fold = collapsed[plan.plan_id] ?? terminal;
        const done = plan.items.filter((i) => i.status === "done").length;
        const failed = plan.items.filter((i) => (i.status === "failed" || i.status === "stopped")).length;
        return (
          <div
            key={plan.plan_id}
            className={`rounded-lg border px-3 py-2 text-xs ${
              plan.status === "pending_confirm"
                ? "border-amber/40 bg-amber/[0.05]"
                : terminal
                  ? "border-line bg-card/40 opacity-80"
                  : "border-violet/30 bg-violet/[0.04]"
            }`}
          >
            <div className="flex items-center gap-1.5">
              <button
                onClick={() => setCollapsed((c) => ({ ...c, [plan.plan_id]: !fold }))}
                className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                title={fold ? t("planBoard.expand") : t("planBoard.collapse")}
              >
                {fold ? (
                  <ChevronRight className="h-3.5 w-3.5 shrink-0 text-faint" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 text-faint" />
                )}
                <ClipboardList className="h-3.5 w-3.5 shrink-0 text-violet" />
                <span className="min-w-0 flex-1 truncate font-medium text-txt" title={plan.title}>
                  {plan.title}
                </span>
              </button>
              <span className="shrink-0 tabular-nums text-faint">
                {t("planBoard.progress", { done, total: plan.items.length })}
                {failed > 0 && t("planBoard.failedCount", { count: failed })}
              </span>
              <span className="shrink-0 rounded-md border border-line2 bg-card2/60 px-1.5 py-0.5 text-[10px] text-muted">
                {t(PLAN_STATUS_KEY[plan.status ?? "active"])}
              </span>
            </div>

            {!fold && (
              <div className="mt-2 flex flex-col gap-1.5">
                {plan.items.map((it) => {
                  // blocked 是派生态（设计 §2）：pending 但依赖未全 done —— 快照
                  // 不落盘，前端按同一条规则推导。
                  const byId = new Map(plan.items.map((x) => [x.item_id, x.status]));
                  const blocked =
                    it.status === "pending" &&
                    (it.depends_on ?? []).some((d) => byId.get(d) !== "done");
                  return <PlanItemRow key={it.item_id} item={blocked ? { ...it, status: "blocked" } : it} />;
                })}
              </div>
            )}

            {plan.status === "pending_confirm" ? (
              <div className="mt-2 flex items-center gap-2 border-t border-line/60 pt-1.5">
                <button
                  onClick={() => void act(plan, "confirm")}
                  disabled={!!busy[plan.plan_id]}
                  className="flex items-center gap-1 rounded-md bg-violet px-2 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  {busy[plan.plan_id] === "confirm" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("planBoard.confirm", { count: plan.items.length })}
                </button>
                <button
                  onClick={() => void act(plan, "cancel")}
                  disabled={!!busy[plan.plan_id]}
                  className="rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-red/40 hover:text-red disabled:opacity-50"
                >
                  {busy[plan.plan_id] === "cancel" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("planBoard.cancel")}
                </button>
                <span className="ml-auto text-[10px] text-faint">{t("planBoard.pendingHint")}</span>
              </div>
            ) : plan.status === "active" ? (
              <div className="mt-2 flex items-center gap-2 border-t border-line/60 pt-1.5">
                <button
                  onClick={() => void act(plan, "cancel")}
                  disabled={!!busy[plan.plan_id]}
                  className="rounded-md border border-line2 px-2 py-1 text-[11px] text-muted transition-colors hover:border-red/40 hover:text-red disabled:opacity-50"
                >
                  {busy[plan.plan_id] === "cancel" && <Loader2 className="h-3 w-3 animate-spin" />}
                  {t("planBoard.cancel")}
                </button>
                <span className="ml-auto text-[10px] text-faint">{t("planBoard.amendHint")}</span>
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function PlanItemRow({ item }: { item: PlanItem }) {
  const g = useGinno();
  const t = useTranslations("chat");
  const meta = ITEM_META[item.status] ?? ITEM_META.pending;
  const clickable = !!item.child_session_id;
  const detail = item.status === "failed" || item.status === "stopped" ? item.error || item.result_summary : item.result_summary;
  return (
    <button
      disabled={!clickable}
      onClick={() => item.child_session_id && g.setActiveSession(item.child_session_id)}
      title={clickable ? t("planBoard.openChild") : undefined}
      className={`w-full rounded-md border border-line bg-card/70 px-2.5 py-1.5 text-left transition-colors ${
        clickable ? "cursor-pointer hover:border-violet/40" : "cursor-default"
      }`}
    >
      <div className="flex items-center gap-1.5">
        {/* 鸭子状态点（设计 §1「复用五态/鸭子三色」）：三桶态用动画鸭；pending/
            blocked 无对应桶，保留灰点 glyph。 */}
        {item.status === "pending" || item.status === "blocked" ? (
          <span className="shrink-0 text-[11px] leading-none" style={{ color: meta.color }}>
            {meta.glyph}
          </span>
        ) : (
          <DuckStatusIcon
            size={14}
            status={
              item.status === "done"
                ? "done"
                : item.status === "failed" || item.status === "stopped"
                  ? "failed"
                  : "running"
            }
          />
        )}
        <span className="shrink-0 rounded bg-violet/10 px-1 py-px font-medium text-violet">{item.item_id}</span>
        <span className="min-w-0 flex-1 truncate text-muted" title={item.goal}>
          {item.goal}
        </span>
        {(item.depends_on?.length ?? 0) > 0 && (
          <span className="shrink-0 text-[10px] text-faint" title={t("planBoard.dependsTitle", { ids: item.depends_on!.join(", ") })}>
            ⇐ {item.depends_on!.join(" ⇐ ")}
          </span>
        )}
        {item.backend && item.backend !== "ginno" && (
          <span className="shrink-0 rounded bg-amber/10 px-1 py-px text-[10px] text-amber">{item.backend}</span>
        )}
        {(item.attempts ?? 0) > 1 && (
          <span className="shrink-0 text-[10px] text-faint">{t("planBoard.attempts", { n: item.attempts! })}</span>
        )}
      </div>
      {detail && (
        <div className="mt-1 line-clamp-1 break-all text-[11px] leading-relaxed text-faint" title={detail}>
          {detail}
        </div>
      )}
    </button>
  );
}
