import { useEffect, useMemo, useState } from "react";
import * as api from "@/lib/runtime";
import type { ContextState, SessionUsage } from "@/lib/types";

export function useRuntimeStale() {
  // P0-2 运行时更新提示：bundle 在进程启动后被替换 → 提示重启。
  const [runtimeStale, setRuntimeStale] = useState(false);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const h = await api.health();
        if (alive) setRuntimeStale(!!(h as { runtime_stale?: boolean }).runtime_stale);
      } catch {
        /* sidecar down — the reconnect banner covers this case */
      }
    };
    void check();
    const id = setInterval(check, 60_000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return { runtimeStale, dismissRuntimeStale: () => setRuntimeStale(false) };
}

export function useShellSessionUsage(activeSessionId: string | null) {
  // Session-cumulative model usage (world-state-plan D2/D3), rendered as a
  // small counter in the TopBar. 两段式（2026-10-31 飘数修复）：
  //   base  = 切会话时 GET /usage 的全树口径（持久 log 聚合，含子孙会话、
  //           跨重启存活）
  //   delta = 本连接期间 WS `usage` 事件的 per-turn 增量累加
  // 显示 base+delta。此前直接用 WS 事件的 `session` 字段（runtime 进程内存
  // 累计、仅本会话）整段覆盖 → 一跑 turn 数字就回退（切会话 100k → 本会话
  // acc 5k），重启后更是从 0 重新爬——「有点飘」的根因。turn 与 base 无
  // 双计：每次 turn 同时落持久 log，下次 GET 的 base 已含历史。
  const [usageBase, setUsageBase] = useState<SessionUsage | null>(null);
  const [usageDelta, setUsageDelta] = useState<SessionUsage | null>(null);
  // 上下文占用（TopBar 圆环）：当前状态而非累计——切会话时 GET 拉初始值，
  // 每轮 WS usage 事件的 context 字段直接替换。
  const [contextState, setContextState] = useState<ContextState | null>(null);
  const usage = useMemo(() => {
    if (!usageBase && !usageDelta) return null;
    if (!usageBase) return usageDelta;
    if (!usageDelta) return usageBase;
    return {
      input_tokens: usageBase.input_tokens + usageDelta.input_tokens,
      output_tokens: usageBase.output_tokens + usageDelta.output_tokens,
      cache_read_tokens: (usageBase.cache_read_tokens ?? 0) + (usageDelta.cache_read_tokens ?? 0),
      cache_creation_tokens:
        (usageBase.cache_creation_tokens ?? 0) + (usageDelta.cache_creation_tokens ?? 0),
      calls: usageBase.calls + usageDelta.calls,
    };
  }, [usageBase, usageDelta]);

  useEffect(() => {
    let alive = true;
    setUsageBase(null);
    setUsageDelta(null);
    setContextState(null);
    if (activeSessionId) {
      api
        .getSessionUsage(activeSessionId)
        .then((r) => {
          if (alive) {
            setUsageBase(r?.usage ?? null);
            setContextState((r as { context?: ContextState })?.context ?? null);
          }
        })
        .catch(() => {
          /* sidecar down — live events will populate later */
        });
    }
    return () => {
      alive = false;
    };
  }, [activeSessionId]);

  return { usage, setUsageDelta, contextState, setContextState };
}
