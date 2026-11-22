/**
 * scheduled/shared.ts 生命周期判定单测（schedule-integrity）。
 *
 * 核心回归：**单次任务暂停未执行不能 Done**——旧版把 once && !enabled 当
 * 「已完成」，用户暂停一个从未按计划跑过的 once 任务也挂绿色 Done；
 * manual（立即执行）的成功写进 last_run，同样被误读。onceOutcome 只认
 * last_scheduled（按计划生命周期：schedule/catch_up 触发 + missed 记账），
 * 老数据无该字段时按 last_run 回退（旧行为兼容，不更糟）。
 */
import { describe, expect, it } from "vitest";

import type { ScheduleTask } from "@/lib/types";
import { isNextRunOverdue, MAX_AUTO_RETRIES, nextRetryDelayMs, onceOutcome } from "./shared";

function onceTask(over: Partial<Pick<ScheduleTask, "last_run" | "last_scheduled" | "enabled">>) {
  return {
    id: "t1",
    name: "T",
    target: { type: "prompt" as const, prompt: "p" },
    schedule: { kind: "once" as const, at: "2026-11-01T09:00:00" },
    enabled: true,
    created: 1,
    updated: 1,
    ...over,
  };
}

describe("onceOutcome：按计划生命周期判定", () => {
  it("暂停未执行（无任何元数据）→ paused，绝不 Done", () => {
    expect(onceOutcome(onceTask({ enabled: false }))).toBe("paused");
  });

  it("manual 成功后手动暂停（last_run ok 无 trigger 元数据、无 last_scheduled）→ trigger=manual 明确是手动 → paused；trigger 缺失 → 来源未知（不绿）", () => {
    // 新路径：manual run 的 last_run 带 trigger=manual 且不写 last_scheduled？
    // 后端 manual 只写 last_run（last_scheduled 键不落）——与老数据同为
    // undefined。为把 manual 与老数据区分，新 runtime 的任务在创建时即落
    // last_scheduled:null，manual 不动它 → paused。
    expect(onceOutcome(onceTask({ enabled: false, last_scheduled: null, last_run: { run_id: "r1", status: "ok", trigger: "manual" } }))).toBe("paused");
    // 中间态：老任务升级后先跑过一次 manual（last_run 带 trigger，任务仍缺
    // last_scheduled 键）→ 明确是手动，同样不算完成。
    expect(onceOutcome(onceTask({ enabled: false, last_run: { run_id: "r1", status: "ok", trigger: "manual" } }))).toBe("paused");
  });

  it("按计划成功（schedule 触发）→ done", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "ok", trigger: "schedule" } })),
    ).toBe("done");
  });

  it("补跑排队（caught_up）→ catchingUp，不是 done——scheduler 对窗口内错过点先记 status=\"caught_up\"（排队标记），真正结论由随后的 trigger_task → _finish_run 写成 ok/error；进程在两步之间停摆时 last_scheduled 停在 caught_up，无任何成功证据", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "caught_up", trigger: "catch_up" } })),
    ).toBe("catchingUp");
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "caught_up", trigger: "schedule" } })),
    ).toBe("catchingUp");
  });

  it("按计划失败（自动停用）→ failed，不算完成", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "error", trigger: "schedule" } })),
    ).toBe("failed");
  });

  it("错过被自动停用 → missed（区别于用户暂停）", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "missed", trigger: "schedule" } })),
    ).toBe("missed");
  });

  it("按计划到点但重叠被跳过 → skipped", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "skipped_overlap", trigger: "schedule" } })),
    ).toBe("skipped");
  });

  it("按计划执行进行中 → running（不抢终态结论）", () => {
    expect(
      onceOutcome(onceTask({ enabled: false, last_scheduled: { run_id: "r1", status: "running", trigger: "schedule" } })),
    ).toBe("running");
  });

  it("新任务显式 last_scheduled:null → paused（不是 legacyDone）", () => {
    expect(onceOutcome(onceTask({ enabled: true, last_scheduled: null }))).toBe("paused");
  });

  it("老数据回退收口（2026-11-22）：无 last_scheduled 时必须「来源 + 成功证据」齐全才 legacyDone——trigger=schedule 且 status=ok", () => {
    // 完整证据：按计划来源 + 成功 → 旧版绿色 Done 维持
    expect(onceOutcome(onceTask({ enabled: false, last_run: { run_id: "r1", status: "ok", trigger: "schedule" } }))).toBe("legacyDone");
  });

  it("老数据 last_run 无来源（trigger 缺失，老版本 schema）→ unknownSource，绝不 Done——「来源未知/已暂停」", () => {
    // 无 trigger：无从证明是计划触发（可能是手动跑的），不绿
    expect(onceOutcome(onceTask({ enabled: false, last_run: { run_id: "r1", status: "ok" } }))).toBe("unknownSource");
    // 无 trigger 且失败：同样来源未知（也不是 failed——那是 last_scheduled 的语义）
    expect(onceOutcome(onceTask({ enabled: false, last_run: { run_id: "r1", status: "error" } }))).toBe("unknownSource");
  });

  it("老数据 last_run trigger=catch_up（有来源非 schedule）→ 不按 legacyDone 放行，来源未知处理", () => {
    // catch_up 也是按计划生命周期，但老数据缺 last_scheduled 时无从确认
    // 终态（caught_up 可能停在排队），保守按来源未知。
    expect(onceOutcome(onceTask({ enabled: false, last_run: { run_id: "r1", status: "ok", trigger: "catch_up" } }))).toBe("unknownSource");
  });

  it("老数据且从未执行 → paused", () => {
    expect(onceOutcome(onceTask({ enabled: false }))).toBe("paused");
  });

  it("非 once 计划 → 恒 paused（onceOutcome 只回答单次生命周期）", () => {
    const daily = { ...onceTask({ last_scheduled: { run_id: "r1", status: "ok", trigger: "schedule" } }), schedule: { kind: "daily" as const, at: "09:30" } };
    expect(onceOutcome(daily)).toBe("paused");
  });
});

describe("isNextRunOverdue：旧数据过期提示", () => {
  const NOW = 1_800_000_000;

  it("未来点 / 刚到点（GRACE 60s + 一个 tick 30s 内）不算过期", () => {
    expect(isNextRunOverdue(NOW + 300, NOW)).toBe(false);
    expect(isNextRunOverdue(NOW - 30, NOW)).toBe(false);
    expect(isNextRunOverdue(NOW - 90, NOW)).toBe(false);
  });

  it("超过 90s 仍是过去点 → 过期（调度器理应已触发/记账）", () => {
    expect(isNextRunOverdue(NOW - 91, NOW)).toBe(true);
    expect(isNextRunOverdue(NOW - 86400, NOW)).toBe(true);
  });

  it("null/undefined（暂停、单次完成）→ 不过期", () => {
    expect(isNextRunOverdue(null, NOW)).toBe(false);
    expect(isNextRunOverdue(undefined, NOW)).toBe(false);
  });
});

describe("nextRetryDelayMs：首载失败的有界退避（复核缺口③）", () => {
  it("1s 起指数翻倍：1s → 2s → 4s → 8s → 16s", () => {
    expect(nextRetryDelayMs(1)).toBe(1000);
    expect(nextRetryDelayMs(2)).toBe(2000);
    expect(nextRetryDelayMs(3)).toBe(4000);
    expect(nextRetryDelayMs(4)).toBe(8000);
    expect(nextRetryDelayMs(5)).toBe(16000);
  });

  it("30s 封顶", () => {
    expect(nextRetryDelayMs(6)).toBe(30000);
    expect(nextRetryDelayMs(MAX_AUTO_RETRIES)).toBe(30000);
  });

  it("attempt ≤0（未在重试）或 > MAX_AUTO_RETRIES（放弃）→ 0（不再自动排定时器）", () => {
    expect(nextRetryDelayMs(0)).toBe(0);
    expect(nextRetryDelayMs(-1)).toBe(0);
    expect(nextRetryDelayMs(MAX_AUTO_RETRIES + 1)).toBe(0);
    expect(nextRetryDelayMs(99)).toBe(0);
  });
});
