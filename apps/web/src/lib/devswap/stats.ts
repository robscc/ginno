// devswap/stats — 阶段时长的 EMA/P90 自适应统计（design §4.2/§4.3）。
//
// 前端 localStorage 存 `ginno:devswap-stats`（goal 约束：不动后端，
// 不落 ~/.ginno/devswap-stats.json）。首次用经验值，之后按本机实测均值
// 自适应；某阶段超过历史 P90 显示「比往常慢」。纯逻辑与存储解耦，
// storage 以参数注入（node 测试无 localStorage）。

import { DEVSWAP_STAGES, type DevSwapStage } from "./types";

export const DEVSWAP_STATS_KEY = "ginno:devswap-stats";

/** 经验首值（秒），来自 design §3 预期时长列。 */
export const DEVSWAP_DEFAULT_SECONDS: Record<DevSwapStage, number> = {
  quiesce: 1,
  unit: 90,
  build_runtime: 240,
  preflight: 20,
  switch: 1,
  finish: 10,
};

export interface StageStat {
  ema: number;
  n: number;
  samples: number[];
}

export type DevSwapStats = Partial<Record<DevSwapStage, StageStat>>;

export interface StorageLike {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
}

const EMA_ALPHA = 0.3;
const MAX_SAMPLES = 20;

function browserStorage(): StorageLike | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* private mode 等异常下退化为纯经验值 */
  }
  return null;
}

export function loadStats(storage: StorageLike | null = browserStorage()): DevSwapStats {
  if (!storage) return {};
  try {
    const raw = storage.getItem(DEVSWAP_STATS_KEY);
    if (!raw) return {};
    const j = JSON.parse(raw) as DevSwapStats;
    if (!j || typeof j !== "object") return {};
    const out: DevSwapStats = {};
    for (const s of DEVSWAP_STAGES) {
      const e = j[s];
      if (
        e &&
        typeof e.ema === "number" &&
        e.ema > 0 &&
        Array.isArray(e.samples)
      ) {
        out[s] = {
          ema: e.ema,
          n: typeof e.n === "number" ? e.n : e.samples.length,
          samples: e.samples.filter((x) => typeof x === "number" && x > 0).slice(-MAX_SAMPLES),
        };
      }
    }
    return out;
  } catch {
    return {};
  }
}

export function saveStats(stats: DevSwapStats, storage: StorageLike | null = browserStorage()) {
  if (!storage) return;
  try {
    storage.setItem(DEVSWAP_STATS_KEY, JSON.stringify(stats));
  } catch {
    /* 配额满/禁写时静默——统计是锦上添花 */
  }
}

/** 记录一次阶段实测时长：EMA(α=0.3) + 滚动样本窗（P90 用）。 */
export function recordStageSeconds(
  stage: DevSwapStage,
  seconds: number,
  storage: StorageLike | null = browserStorage(),
): DevSwapStats {
  if (!(seconds > 0) || !Number.isFinite(seconds)) return loadStats(storage);
  const stats = loadStats(storage);
  const prev = stats[stage];
  const samples = prev ? [...prev.samples, seconds].slice(-MAX_SAMPLES) : [seconds];
  const ema = prev ? prev.ema * (1 - EMA_ALPHA) + seconds * EMA_ALPHA : seconds;
  const next: DevSwapStats = { ...stats, [stage]: { ema, n: (prev?.n ?? 0) + 1, samples } };
  saveStats(next, storage);
  return next;
}

/** 线性插值 P90（样本按需排序；样本 <2 时退回 EMA/经验值）。 */
export function percentile90(samples: number[]): number {
  if (!samples.length) return 0;
  const s = [...samples].sort((a, b) => a - b);
  const idx = Math.min(s.length - 1, Math.ceil(0.9 * s.length) - 1);
  return s[idx];
}

export interface StageExpectation {
  /** 展示用期望时长（秒）：有历史用 EMA，无历史用经验值。 */
  ema: number;
  /** 慢告警阈值（秒）：≥2 个样本时为历史 P90，否则 = ema（不告警）。 */
  p90: number;
  /** 样本是否足够给出 P90 告警。 */
  hasP90: boolean;
}

export function expectationFor(
  stage: DevSwapStage,
  stats: DevSwapStats = loadStats(),
): StageExpectation {
  const st = stats[stage];
  if (st && st.samples.length >= 2) {
    return { ema: st.ema, p90: percentile90(st.samples), hasP90: true };
  }
  const fallback = st?.ema ?? DEVSWAP_DEFAULT_SECONDS[stage];
  return { ema: fallback, p90: fallback, hasP90: false };
}

// ── 纯展示格式化（DevSwapCard 与 session 状态行共用）─────────────────────

/** m:ss 时钟。 */
export function fmtClock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 5 格 ASCII 进度条（design §4 示意样式 ▓▓▓░░）。pct 缺失 → null。 */
export function progressBar(pct: number | null | undefined): string {
  if (typeof pct !== "number" || !Number.isFinite(pct)) return "";
  const filled = Math.max(0, Math.min(5, Math.round((pct / 100) * 5)));
  return "▓".repeat(filled) + "░".repeat(5 - filled);
}

/** session 内等待行的单行文案素材（i18n 在组件里拼，这里只算数）。 */
export interface WaitLineData {
  bar: string;
  pctLabel: string;
  elapsedLabel: string;
  etaLabel: string;
  slow: boolean;
}

export function waitLineData(
  stageElapsed: number,
  pct: number | null | undefined,
  exp: StageExpectation,
): WaitLineData {
  const bar = progressBar(pct);
  const slow = exp.hasP90 && stageElapsed > exp.p90;
  // 超出 EMA 期望后不再显示「剩 0:00」（2026-11-04 用户反馈：挂死感），
  // 改由 slow 标志驱动「比往常慢」文案接管。
  const etaLabel =
    exp.ema - stageElapsed > 0 ? fmtClock(exp.ema - stageElapsed) : "";
  return {
    bar,
    pctLabel: typeof pct === "number" ? `${Math.round(pct)}%` : "",
    elapsedLabel: fmtClock(stageElapsed),
    etaLabel,
    slow,
  };
}
