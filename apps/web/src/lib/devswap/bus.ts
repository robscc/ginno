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
  latest = run;
  emit();
}

export function getDevSwapRun(): DevSwapRun | null {
  return latest;
}

export function clearDevSwapRun() {
  lastKey = "";
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
  if (run) pushDevSwapRun(run);
  else clearDevSwapRun();
  return run;
}
