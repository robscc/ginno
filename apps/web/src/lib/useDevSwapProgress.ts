"use client";

// useDevSwapProgress — dev-swap 进度 hook（design §5）。
//
// 数据源：模块级 bus（handle.ts 的 runtime.devswap_progress case 推入）。
// **本 hook 只订阅、不拉快照**——它被每个聊天气泡挂载（状态行），挂载即
// fetch 会在 reload/切会话时打出几十个并发 GET（2026-11-04 日志实锤）。
// 快照拉取只在两处：DevSwapCard 挂载一次（页面 reload 后 bus 是空的）+
// useChatStreamEngine 的 sock.onopen（重连续播，design §5）。
//
// 附带统计副作用：run 阶段推进/结束时把上一阶段的实测秒数写进
// ginno:devswap-stats（EMA + 样本窗，见 devswap/stats.ts）。

import { useEffect, useRef, useState } from "react";
import { getDevSwapRun, subscribeDevSwap } from "./devswap/bus";
import { recordStageSeconds } from "./devswap/stats";
import { DEVSWAP_STAGES, type DevSwapRun } from "./devswap/types";

export interface DevSwapProgress {
  run: DevSwapRun | null;
}

export function useDevSwapProgress(): DevSwapProgress {
  const [run, setRun] = useState<DevSwapRun | null>(getDevSwapRun);
  // 阶段计时基准：{stage, startedAt}。事件不带每阶段起始时间（契约只有
  // 当前态），以客户端观测到的切换时刻为准——够 EMA 用。
  const timingRef = useRef<{ stage: DevSwapRun["stage"]; at: number } | null>(null);
  const lastRunRef = useRef<DevSwapRun | null>(run);

  useEffect(() => {
    let alive = true;
    const unsub = subscribeDevSwap((r) => {
      if (alive) setRun(r);
    });
    return () => {
      alive = false;
      unsub();
    };
  }, []);

  // 阶段时长统计：观测到 stage 切换 / run 终态时落一笔上一阶段的时长。
  useEffect(() => {
    const prev = lastRunRef.current;
    lastRunRef.current = run;
    if (!run) {
      timingRef.current = null;
      return;
    }
    const timing = timingRef.current;
    const prevIdx = prev ? DEVSWAP_STAGES.indexOf(prev.stage) : -1;
    const curIdx = DEVSWAP_STAGES.indexOf(run.stage);
    const sameRun = prev && prev.run_id === run.run_id;
    const stageAdvanced = sameRun && curIdx > prevIdx;
    const ended = sameRun && run.state !== "running" && prev.state === "running";

    if (stageAdvanced && timing) {
      recordStageSeconds(timing.stage, (Date.now() - timing.at) / 1000);
      timingRef.current = { stage: run.stage, at: Date.now() };
    } else if (ended && timing) {
      recordStageSeconds(timing.stage, (Date.now() - timing.at) / 1000);
      timingRef.current = null;
    } else if (!timing || !sameRun) {
      timingRef.current = { stage: run.stage, at: Date.now() };
    }
  }, [run]);

  return { run };
}
