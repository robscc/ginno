/**
 * /api/tasks/counts 的 API 客户端 —— 常驻徽标（SidebarHeader ▶N / RightPanel
 * 「运行」tab）专用的轻量计数端点。明细枚举走 ./tasksOverview（打开面板才
 * 付全量解析的成本）；两者服务端共享同一批真源，counts 形状与 overview
 * 载荷里的 `counts` 字段一致。
 */

import { BASE, H } from "./core";

/** 六源计数 + total（total 恒等于六组之和，见 normalizeTasksCounts）。 */
export interface TasksCounts {
  turns: number;
  scheduled: number;
  subagents: number;
  runs: number;
  goals: number;
  delegations: number;
  total: number;
}

const SIX_KEYS = [
  "turns",
  "scheduled",
  "subagents",
  "runs",
  "goals",
  "delegations",
] as const;

export function emptyTasksCounts(): TasksCounts {
  return { turns: 0, scheduled: 0, subagents: 0, runs: 0, goals: 0, delegations: 0, total: 0 };
}

/** 防御性归一：非有限/负数一律 0；total 以六组之和重算——徽标数字恒等于
 * 各部分之和，杜绝「total 与明细漂移」的展示口径问题。 */
export function normalizeTasksCounts(raw: unknown): TasksCounts {
  const root = (raw ?? {}) as Record<string, unknown>;
  const c = (root.counts ?? root) as Record<string, unknown>;
  const out = emptyTasksCounts();
  for (const key of SIX_KEYS) {
    const v = c[key];
    if (typeof v === "number" && Number.isFinite(v) && v > 0) {
      out[key] = Math.floor(v);
    }
  }
  out.total = SIX_KEYS.reduce((n, key) => n + out[key], 0);
  return out;
}

/** 拉一次计数。非 2xx（旧 sidecar 404 / sidecar 未起）抛错，调用方兜底。 */
export async function getTasksCounts(): Promise<TasksCounts> {
  const res = await fetch(`${BASE}/tasks/counts`, { headers: H });
  if (!res.ok) throw new Error(`tasks/counts ${res.status}`);
  return normalizeTasksCounts(await res.json());
}
