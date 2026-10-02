// scheduled 页各面板共用的展示辅助（scheduled-tasks-design.md §3.4/§3.5）：
// 计划描述、状态元数据、时间格式化。只放纯函数，不碰 React。

import type { SchedulePlan, ScheduleRun } from "@/lib/types";

const pad = (n: number) => String(n).padStart(2, "0");
export const WEEKDAY_LABEL = ["日", "一", "二", "三", "四", "五", "六"];

/** 计划的人类描述（§3.2 任务卡第二段）：每 30 分钟 / 每天 09:30 / 每周五 18:00 / 单次。 */
export function planDescription(p: SchedulePlan): string {
  switch (p.kind) {
    case "interval":
      return p.minutes % 60 === 0 ? `每 ${p.minutes / 60} 小时` : `每 ${p.minutes} 分钟`;
    case "daily":
      return `每天 ${p.at}`;
    case "weekly":
      return `每周${WEEKDAY_LABEL[((p.weekday % 7) + 7) % 7]} ${p.at}`;
    case "once": {
      const d = new Date(p.at);
      if (isNaN(d.getTime())) return `单次 ${p.at}`;
      return `单次 ${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
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
  if (diffDays === 0) return "今天";
  if (diffDays === 1) return "昨天";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
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

/** 执行状态元数据（§3.4 时间条色 + §3.5 记录列）。missed 画空心，无实色。 */
export const RUN_STATUS_META: Record<
  string,
  { label: string; color: string; hollow?: boolean; glyph: string }
> = {
  ok: { label: "成功", color: "#14b8a6", glyph: "✓" }, // 青绿
  running: { label: "进行中", color: "#14b8a6", glyph: "▨" },
  error: { label: "失败", color: "#ef4444", glyph: "✕" },
  skipped_overlap: { label: "跳过(重叠)", color: "#71717a", glyph: "◌" },
  missed: { label: "错过", color: "#71717a", hollow: true, glyph: "◌" },
};

export function statusMeta(status: string) {
  return (
    RUN_STATUS_META[status] ?? { label: status, color: "#71717a", glyph: "·" }
  );
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
