/**
 * /api/tasks/counts 的 API 客户端 —— 常驻徽标（右栏「运行」tab）专用的轻量计数端点。明细枚举走 ./tasksOverview（打开面板才
 * 付全量解析的成本）；两者服务端共享同一批真源，counts 形状与 overview
 * 载荷里的 `counts` 字段一致。
 */

import { BASE, H } from "./core";

/** 三档分类计数（2026-11-21 分类修订）：执行中 / 等你处理 / 已排期。
 * 服务端按真源推导（parked turn → waiting）；旧 sidecar 无此字段时为
 * undefined，徽标退回纯总数文案。 */
export interface TasksCountPhases {
  running: number;
  waiting: number;
  scheduled: number;
}

/** 六源计数 + total（total 恒等于六组之和，见 normalizeTasksCounts）。 */
export interface TasksCounts {
  turns: number;
  scheduled: number;
  subagents: number;
  runs: number;
  goals: number;
  delegations: number;
  total: number;
  /** 三档分类计数；仅新 sidecar 返回（缺省 = 旧 sidecar，UI 退回总数）。 */
  phases?: TasksCountPhases;
}

const SIX_KEYS = [
  "turns",
  "scheduled",
  "subagents",
  "runs",
  "goals",
  "delegations",
] as const;

const PHASE_KEYS = ["running", "waiting", "scheduled"] as const;

export function emptyTasksCounts(): TasksCounts {
  return { turns: 0, scheduled: 0, subagents: 0, runs: 0, goals: 0, delegations: 0, total: 0 };
}

/** 防御性归一：非有限/负数一律 0；total 以六组之和重算——徽标数字恒等于
 * 各部分之和，杜绝「total 与明细漂移」的展示口径问题。phases 仅在三项
 * 齐全且合法时透传（部分缺失按无此字段处理，UI 退回总数文案）。 */
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
  const rawPhases = c.phases as Record<string, unknown> | undefined;
  if (rawPhases && typeof rawPhases === "object") {
    const phases: TasksCountPhases = { running: 0, waiting: 0, scheduled: 0 };
    let complete = true;
    for (const key of PHASE_KEYS) {
      const v = rawPhases[key];
      if (typeof v === "number" && Number.isFinite(v) && v >= 0) {
        phases[key] = Math.floor(v);
      } else {
        complete = false;
        break;
      }
    }
    if (complete) out.phases = phases;
  }
  return out;
}

/** 拉一次计数。非 2xx（旧 sidecar 404 / sidecar 未起）抛错，调用方兜底。 */
export async function getTasksCounts(): Promise<TasksCounts> {
  const res = await fetch(`${BASE}/tasks/counts`, { headers: H });
  if (!res.ok) throw new Error(`tasks/counts ${res.status}`);
  return normalizeTasksCounts(await res.json());
}
