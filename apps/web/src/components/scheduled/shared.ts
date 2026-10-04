// scheduled 页各面板共用的展示辅助（scheduled-tasks-design.md §3.4/§3.5）：
// 计划描述、状态元数据、时间格式化。只放纯函数，不碰 React。
// 用户可见文案经 i18n/schedText（sched 域 catalog + locale 镜像）读取，
// 与 useTranslations("sched") 同名同源。

import type { SchedulePlan, ScheduleRun } from "@/lib/types";
import { schedText } from "@/i18n/schedText";

const pad = (n: number) => String(n).padStart(2, "0");

/** 周几 → catalog key 后缀（与 sched.weekday.* 一一对应）。 */
export const WEEKDAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"] as const;

function weekdayName(i: number): string {
  return schedText(`weekday.${WEEKDAY_KEYS[((i % 7) + 7) % 7]}`);
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
    case "weekly":
      return schedText("plan.weekly", { day: weekdayName(p.weekday), at: p.at });
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

/** YYYY-MM-DD（本地时区）——timeline/runs 接口的 date 参数。 */
export function dateStr(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
