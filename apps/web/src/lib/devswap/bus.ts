// devswap/bus — `runtime.devswap_progress` 的前端接收总线（模块级单例）。
//
// 接线惯例对齐 code.changed：事件在 per-session WS 的 handle.ts 分发器里
// 命中 case 后推到本总线，hook 订阅总线而不是自己开 socket。设计文档
// docs/dev-swap-progress-design.md §5：WS 断线重连（壳收尾会断 WS）后
// 先拉一次 GET /api/devswap/progress 快照再续订——resyncDevSwapRun()
// 就是这个动作，useChatStreamEngine 在 sock.onopen 里调用它。

import { BASE } from "../api/core";
import { normalizeDevSwapRun, type DevSwapRun } from "./types";

type Listener = (run: DevSwapRun | null) => void;

let latest: DevSwapRun | null = null;
// (run_id|stage) → 该阶段首次看到的墙钟秒。elapsed 计时锚点：同一阶段
// 的后续事件（pct 递增等）复用它，而不是各自的事件 ts。
const stageStart = new Map<string, number>();
// run_id+stage+state+pct 去重键：同一事件经多会话 socket 重复投递/快照
// 回放时静默吞掉，不触发多余渲染。
let lastKey = "";
const listeners = new Set<Listener>();

function emit() {
  for (const fn of listeners) fn(latest);
}

export function pushDevSwapRun(run: DevSwapRun) {
  const key = `${run.run_id}|${run.stage}|${run.state}|${run.pct ?? ""}`;
  if (key === lastKey) return;
  lastKey = key;
  const sk = `${run.run_id}|${run.stage}`;
  let started = stageStart.get(sk);
  if (started === undefined) {
    started = run.ts ?? Math.floor(Date.now() / 1000);
    stageStart.set(sk, started);
    // 防泄漏：只留最近 16 个键（一个 run 最多 6 阶段，两轮 run 足够）。
    if (stageStart.size > 16) {
      const first = stageStart.keys().next().value;
      if (first !== undefined) stageStart.delete(first);
    }
  }
  latest = { ...run, stageStartedAt: started };
  emit();
}

export function getDevSwapRun(): DevSwapRun | null {
  return latest;
}

export function clearDevSwapRun() {
  lastKey = "";
  stageStart.clear();
  if (latest === null) return;
  latest = null;
  emit();
}

export function subscribeDevSwap(fn: Listener): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

// 结果落块的 run_id 去重（§4.4）：一个 run 只落一次块——live 转移（running→
// 终态）与重载后的快照回放会看到同一终态两次。滑动上限防泄漏。
const deliveredResults = new Set<string>();

export function markDevSwapResultDelivered(runId: string): boolean {
  if (deliveredResults.has(runId)) return false;
  deliveredResults.add(runId);
  if (deliveredResults.size > 8) {
    const first = deliveredResults.values().next().value;
    if (first !== undefined) deliveredResults.delete(first);
  }
  return true;
}

/** GET /api/devswap/progress 快照。端点不存在（旧 runtime / 无进行中 run
 * 返回 404 或空体）时返回 null——静默，不打扰。 */
export async function fetchDevSwapSnapshot(): Promise<DevSwapRun | null> {
  try {
    const res = await fetch(`${BASE}/devswap/progress`);
    if (!res.ok) return null;
    const j: unknown = await res.json();
    if (!j || typeof j !== "object") return null;
    const obj = j as Record<string, unknown>;
    // 后端两种合法形态：直接 run 对象，或 {run: {...}|null}。
    const raw = (obj.run && typeof obj.run === "object" ? obj.run : obj) as Record<
      string,
      unknown
    >;
    return normalizeDevSwapRun(raw);
  } catch {
    return null;
  }
}

/** 断线重连后的续播动作：拉快照，有 run 就推送，没有就清空本地态。 */
export async function resyncDevSwapRun(): Promise<DevSwapRun | null> {
  const run = await fetchDevSwapSnapshot();
  if (run) {
    pushDevSwapRun(run);
    // 兜底（2026-11-05 实案：确认门广播发出时前端断连，确认框永久丢失，
    // finish:running 挂了 57 分钟）：重连拉快照发现 finish 仍在 running
    // 且确认态为空 → 自动重开确认框。真实重启过程中 app 会整页 reload，
    // 不会误弹（重开后的新进程快照是终态/新 run）。
    if (run.stage === "finish" && run.state === "running" && !isDevSwapConfirmPending()) {
      setDevSwapPendingConfirm("pending");
    }
  } else {
    clearDevSwapRun();
  }
  return run;
}

// ---- 切换确认门（2026-11-05 用户需求：确认完才能正式切换） ----
// 构建成功后 CLI 已切 symlink，但「重启 sidecar + 验证」要等用户确认；
// runtime.dev_swap_pending 不再自动 invoke finish_dev_swap。
// 三态（2026-11-05）：null 无待切换 | "pending" 弹确认框 | "postponed"
// 用户点了暂不——确认框收起，但切换并未完成；进度徽标转为可点的
// 「待切换」态（点击 = 补执行 finish_dev_swap），不让用户只能靠重启。
let pendingConfirm: "pending" | "postponed" | null = null;
const confirmSubs = new Set<(v: "pending" | "postponed" | null) => void>();

export function setDevSwapPendingConfirm(v: "pending" | "postponed" | null): void {
  if (pendingConfirm === v) return;
  pendingConfirm = v;
  for (const fn of confirmSubs) fn(v);
}
export function subscribeDevSwapConfirm(
  fn: (v: "pending" | "postponed" | null) => void,
): () => void {
  confirmSubs.add(fn);
  return () => confirmSubs.delete(fn);
}
export function isDevSwapConfirmPending(): "pending" | "postponed" | null {
  return pendingConfirm;
}
