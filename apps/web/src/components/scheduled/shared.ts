// scheduled 页各面板共用的展示辅助（scheduled-tasks-design.md §3.4/§3.5）：
// 计划描述、状态元数据、时间格式化。只放纯函数，不碰 React。
// 用户可见文案经 i18n/schedText（sched 域 catalog + locale 镜像）读取，
// 与 useTranslations("sched") 同名同源。

import type { SchedulePlan, ScheduleRun, ScheduleTask } from "@/lib/types";
import { schedText } from "@/i18n/schedText";

const pad = (n: number) => String(n).padStart(2, "0");

/** 周几 → catalog key 后缀（与 sched.weekday.* 一一对应）。 */
export const WEEKDAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] as const;

function weekdayName(i: number): string {
  return schedText(`weekday.${WEEKDAY_KEYS[((i % 7) + 7) % 7]}`);
}

/** weekly 计划的星期集合归一化：weekdays 多选优先，回落旧单值 weekday（0=周日）。 */
export function weeklyDays(p: Extract<SchedulePlan, { kind: "weekly" }>): number[] {
  if (Array.isArray(p.weekdays) && p.weekdays.length) return [...new Set(p.weekdays)];
  return [p.weekday ?? 0];
}

/** 计划的人类描述（§3.2 任务卡第二段）：每 30 分钟 / 每天 09:30 / 每周五 18:00 / 单次。 */
export function planDescription(p: SchedulePlan): string {
  switch (p.kind) {
    case "interval":
      return p.minutes % 60 === 0
        ? schedText("plan.everyHours", { n: p.minutes / 60 })
        : schedText("plan.everyMinutes", { n: p.minutes });
    case "daily":
      return schedText("plan.daily", { at: p.at });
    case "weekly": {
      const days = weeklyDays(p).slice().sort((a, b) => a - b);
      if (days.length === 1) return schedText("plan.weekly", { day: weekdayName(days[0]), at: p.at });
      if (days.length === 7) return schedText("plan.daily", { at: p.at });
      return schedText("plan.weeklyMulti", { days: days.map(weekdayName).join(schedText("listSep")), at: p.at });
    }
    case "once": {
      const d = new Date(p.at);
      if (isNaN(d.getTime())) return schedText("plan.once", { when: p.at });
      return schedText("plan.once", {
        when: `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`,
      });
    }
  }
}

/** epoch 秒 → 当地 HH:MM。 */
export function fmtClock(sec: number | null | undefined): string {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** epoch 秒 → 「今天 / 昨天 / M月D日」（执行记录的时间列前缀）。 */
export function fmtDayPrefix(sec: number | null | undefined): string {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const today = new Date();
  const dayStart = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((dayStart(today) - dayStart(d)) / 86400000);
  if (diffDays === 0) return schedText("dayPrefix.today");
  if (diffDays === 1) return schedText("dayPrefix.yesterday");
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

/** 秒 → 「1m12s / 3m42s / 1h06m」；无时长返回 —。 */
export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${pad(s % 60)}s`;
  return `${Math.floor(m / 60)}h${pad(m % 60)}m`;
}

/** 执行状态的颜色/图形元数据（§3.4 时间条色 + §3.5 记录列）。missed 画空心，无实色。
 *  显示名（label）走 statusLabel()——catalog 驱动，locale 切换即时生效。 */
const RUN_STATUS_COLOR: Record<string, { color: string; hollow?: boolean; glyph: string }> = {
  ok: { color: "var(--st-ok)", glyph: "✓" }, // 品牌紫(亮色主题自动翻深)
  running: { color: "var(--st-running)", glyph: "▨" },
  error: { color: "var(--st-error)", glyph: "✕" },
  skipped_overlap: { color: "var(--st-skipped)", glyph: "◌" },
  missed: { color: "var(--st-missed)", hollow: true, glyph: "◌" },
};

/** 状态显示名（sched.status.*；未知状态原样显示协议值）。 */
export function statusLabel(status: string): string {
  switch (status) {
    case "ok":
      return schedText("status.ok");
    case "running":
      return schedText("status.running");
    case "error":
      return schedText("status.error");
    case "skipped_overlap":
      return schedText("status.skipped");
    case "missed":
      return schedText("status.missed");
    default:
      return status;
  }
}

export function statusMeta(status: string) {
  const c = RUN_STATUS_COLOR[status] ?? { color: "var(--st-skipped)", glyph: "·" };
  return { label: statusLabel(status), color: c.color, hollow: c.hollow, glyph: c.glyph };
}

/** 该行可否点进回放（§3.5）：missed 无会话/无 run，不可点；skipped 同理。 */
export function runClickable(r: ScheduleRun): boolean {
  if (r.status === "missed" || r.status === "skipped_overlap") return false;
  return r.target_type === "prompt" ? !!r.session_id : !!r.workflow_run_id;
}

/**
 * 回放目标是否**仍然存在**（§3.5 DATA-06）：在 runClickable 之上再排除
 * 「影子会话已被删除」的记录——这类记录 session_id 已被清空并打
 * ``session_missing``，摘要还在，点开会 404。两种「打不开」要区分：
 *   · session_missing=true      → 会话曾经存在、后来被删（可解释为「会话已删除」）
 *   · session_id 为空且未标记   → 失败在创建会话之前（如模型构建失败），从未有回放
 * 两者都不可打开，但 UI 只有前者该保留禁用入口。
 */
export function runReplayable(r: ScheduleRun): boolean {
  if (!runClickable(r)) return false;
  return !(r.target_type === "prompt" && r.session_missing === true);
}

/** YYYY-MM-DD（本地时区）——timeline/runs 接口的 date 参数。 */
export function dateStr(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// ---- 任务生命周期判定（schedule-integrity）-----------------------------------
//
// 核心纪律：**执行结果与 enabled 开关互不推导**。旧版把「once && !enabled」
// 当「已完成」，于是「用户暂停一个还没执行的 once 任务」也挂绿色 Done 徽标；
// 且 manual（立即执行）的成功会写 last_run，同样被误读为按计划完成。
// 后端现已把按计划生命周期（schedule/catch_up 触发 + missed/caught_up 记账）
// 单独落在 last_scheduled，这里只做读侧判定：
//
//   done          按计划成功（once 显示「已完成」的唯一依据）
//   catchingUp    计划点错过且已**排队**补跑——scheduler._consume_missed 对
//                 窗口内的错过点先记 status="caught_up"（这是「正在补跑」
//                 的排队标记，不是结果），真正结论由随后的 trigger_task →
//                 _finish_run 写成 running/ok/error。进程在两步之间停摆时
//                 last_scheduled 会停在 caught_up：它只说明「该点已进入补跑
//                 队列」，**没有任何成功证据**，绝不显示 Done。
//   missed        计划点错过（once 被自动停用 → 「已停用 · 计划点已错过」）
//   failed        按计划执行失败（自动停用 → 红字，不算完成）
//   skipped       按计划触发时重叠被跳过（不算完成）
//   running       按计划执行进行中（此刻任务已自动停用）
//   paused        无按计划事件——手动暂停/未执行/manual 触发（绝不是 Done）
//   legacyDone    **老数据回退**：调度器从未写过 last_scheduled（undefined）
//                 但 last_run 带 trigger=schedule 且 status=ok——来源与成功
//                 证据齐全才算（2026-11-22 收口：此前 trigger 缺失也放行，
//                 无来源的 last_run 被渲染成绿色 Done）。
//   unknownSource 老数据 last_run 存在但无来源（trigger 缺失）或非成功——
//                 无从证明是按计划成功，显示「已暂停 · 来源未知」，不绿。

export type OnceOutcome =
  | "done"
  | "catchingUp"
  | "missed"
  | "failed"
  | "skipped"
  | "running"
  | "paused"
  | "legacyDone"
  | "unknownSource";

export function onceOutcome(t: Pick<ScheduleTask, "schedule" | "last_run" | "last_scheduled">): OnceOutcome {
  if (t.schedule.kind !== "once") return "paused";
  const sched = t.last_scheduled;
  if (sched !== undefined) {
    if (!sched) return "paused"; // 显式 null：新任务尚无按计划事件（manual 不写此键）
    switch (sched.status) {
      case "ok":
        return "done";
      case "missed":
        return "missed";
      case "caught_up":
        // 排队补跑 ≠ 成功（见上）：等待调度恢复，结果未知，不抢「已完成」。
        return "catchingUp";
      case "error":
        return "failed";
      case "skipped_overlap":
        return "skipped";
      case "running":
        return "running"; // 按计划执行进行中（此刻任务已自动停用）
      default:
        return "paused"; // 其他未知中间态：不抢结论
    }
  }
  // 老数据无 last_scheduled：按 last_run 回退，但必须「来源 + 成功证据」
  // 齐全才认按计划完成——trigger=manual 是明确的手动（不算完成）；trigger
  // 缺失/未知（老版本 schema）无从证明是计划触发，一律来源未知（不绿）。
  if (!t.last_run) return "paused";
  if (t.last_run.trigger === "schedule" && t.last_run.status === "ok") return "legacyDone";
  if (t.last_run.trigger === "manual") return "paused";
  return "unknownSource";
}

/** 计划点是否已过期（schedule-integrity「旧数据过期提示」）：启用中的任务
 *  next_run_at 落在过去超过 GRACE+一个 tick（60s+30s=90s）→ 调度器理应已
 *  触发/记账却仍是旧值（如 sidecar 长时间未跑），显示「已过期·等待对账」
 *  而不是倒拼出一个过去时点的「下次：昨天 09:30」。 */
export function isNextRunOverdue(nextRunAt: number | null | undefined, now = Date.now() / 1000): boolean {
  if (!nextRunAt) return false;
  return now - nextRunAt > 90;
}

/** 首载失败的有界退避间隔（schedule-integrity 收口）：第 attempt 次失败后
 *  下一次自动重试等 1s×2^(attempt-1)，封顶 30s；attempt > MAX_AUTO_RETRIES
 *  返回 0 = 停止自动重试（只留手动按钮）。
 *
 *  取代旧实现：旧版 retrying 布尔在第二次失败时 setRetrying(true) 是同值
 *  no-op，effect 依赖不变不重排，setTimeout 全生命周期只排过一次——注释
 *  宣称的「2s 间隔无限重试」从未成立（schedule-integrity 复核缺口③）。 */
export const MAX_AUTO_RETRIES = 8;

export function nextRetryDelayMs(attempt: number): number {
  if (attempt <= 0 || attempt > MAX_AUTO_RETRIES) return 0;
  return Math.min(1000 * 2 ** (attempt - 1), 30000);
}
