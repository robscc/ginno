// devswap/types — dev-swap 热切换进度可视化的共享类型与阶段枚举
// （docs/dev-swap-progress-design.md §1/§2）。事件契约以后端为准：
// WS `runtime.devswap_progress`，payload {run_id,stage,state,detail,pct,log_tail}；
// 快照 GET /api/devswap/progress。

/** 六个固定阶段，序即 dev_swap.sh 的实际执行序（design §3 表）。 */
export const DEVSWAP_STAGES = [
  "quiesce",
  "unit",
  "build_runtime",
  "preflight",
  "switch",
  "finish",
] as const;

export type DevSwapStage = (typeof DEVSWAP_STAGES)[number];

export type DevSwapRunState = "running" | "ok" | "fail" | "rolled_back";

/** 一条进度事件 / 快照的规范化形态（缺省字段允许 null）。 */
export interface DevSwapRun {
  run_id: string;
  stage: DevSwapStage;
  state: DevSwapRunState;
  detail?: string | null;
  /** 仅 build 阶段上报（design §2.1），0-100。 */
  pct?: number | null;
  /** 失败时附带的该阶段尾日志（快照与事件都可带）。 */
  log_tail?: string | null;
  /** 事件到达/快照生成的墙钟（秒，客户端本地补齐用）。 */
  ts?: number;
  /** run 起始墙钟（秒，后端快照带；结果落块算总耗时用）。 */
  startedAt?: number | null;
  /** 当前 stage 的起始墙钟（秒，bus 层按 run_id+stage 维护——事件里
   *  的 ts 是「本条事件到达时刻」，build 阶段 pct 每 2~5s 变一次会把
   *  elapsed 计时反复重置，2026-11-10 用户实录）。 */
  stageStartedAt?: number | null;
  /** 发起会话（/dev-swap 命令路径才有）：session 状态行与结果落块的
   * 会话门控（§4.5 B 会话不打扰）；终端 make dev-swap 无 sid。 */
  sid?: string | null;
}

/** 从任意 WS 事件对象里抽取规范 run（字段缺失/非法时返回 null）。 */
export function normalizeDevSwapRun(ev: {
  [k: string]: unknown;
}): DevSwapRun | null {
  // 后端事件形态 {ok, run:{...}}（广播/GET 同构）；容忍直接平铺的旧形态。
  const src = (ev.run && typeof ev.run === "object" ? ev.run : ev) as {
    [k: string]: unknown;
  };
  const run_id = typeof src.run_id === "string" ? src.run_id : null;
  if (!run_id) return null;
  const stage = DEVSWAP_STAGES.find((s) => s === src.stage);
  const state = src.state;
  if (!stage) return null;
  if (state !== "running" && state !== "ok" && state !== "fail" && state !== "rolled_back")
    return null;
  return {
    run_id,
    stage,
    state,
    detail: typeof src.detail === "string" ? src.detail : null,
    pct: typeof src.pct === "number" ? src.pct : null,
    log_tail: typeof src.log_tail === "string" ? src.log_tail : null,
    ts: typeof src.ts === "number" ? src.ts : Math.floor(Date.now() / 1000),
    startedAt: typeof src.started_at === "number" ? src.started_at : null,
    sid: typeof src.sid === "string" && src.sid ? src.sid : null,
  };
}

/** 单阶段在时间线上的状态（DevSwapCard 渲染用，纯函数）。 */
export type StageCell = "done" | "active" | "fail" | "pending";

/** 六阶段时间线状态：进度是线性的——run.stage 之前视为已完成，
 * stage 本身按 run.state 落点，之后待定。rolled_back 视作失败但由
 * 卡片另出横幅。 */
export function stageCells(run: DevSwapRun): StageCell[] {
  const idx = DEVSWAP_STAGES.indexOf(run.stage);
  const finished = run.state === "ok";
  const failed = run.state === "fail" || run.state === "rolled_back";
  return DEVSWAP_STAGES.map((_, i) => {
    if (finished && i <= idx) return "done";
    if (failed && i === idx) return "fail";
    if (failed && i < idx) return "done";
    if (i < idx) return "done";
    if (i === idx) return "active";
    return "pending";
  });
}

/** run 是否处于活跃（需要展示卡片/状态行）的态。 */
export function isDevSwapActive(run: DevSwapRun | null | undefined): boolean {
  return !!run && run.state === "running";
}
