// devswap 纯逻辑单测（node 环境，无 React）：事件规范化、六阶段时间线
// 状态机、EMA/P90 统计往返、等待行数据。useDevSwapProgress 本体是这些
// 纯函数的薄 React 包装（vitest node 环境约定见 vitest.config.ts）。

import { describe, expect, it } from "vitest";
import {
  normalizeDevSwapRun,
  stageCells,
  isDevSwapActive,
  DEVSWAP_STAGES,
} from "./devswap/types";
import {
  loadStats,
  saveStats,
  recordStageSeconds,
  percentile90,
  expectationFor,
  fmtClock,
  progressBar,
  waitLineData,
  DEVSWAP_DEFAULT_SECONDS,
  type StorageLike,
} from "./devswap/stats";

const RUN = {
  run_id: "a3f9c2e",
  stage: "build_runtime",
  state: "running",
  detail: "PyInstaller …",
  pct: 55,
} as const;

function memStorage(): StorageLike & { dump: () => Record<string, string> } {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, v),
    dump: () => Object.fromEntries(m),
  };
}

describe("normalizeDevSwapRun", () => {
  it("accepts a well-formed event and fills ts", () => {
    const r = normalizeDevSwapRun(RUN);
    expect(r).not.toBeNull();
    expect(r!.run_id).toBe("a3f9c2e");
    expect(r!.stage).toBe("build_runtime");
    expect(r!.state).toBe("running");
    expect(r!.pct).toBe(55);
    expect(typeof r!.ts).toBe("number");
  });

  it("unwraps {ok, run:{...}} backend snapshot shape (契约断裂回归)", () => {
    // 后端广播/GET 的实际形态：{ok, run:{扁平 current-line}}——嵌套快照
    // 时代前端只认顶层字段，事件全被丢弃（2026-11-04 事故）。
    const r = normalizeDevSwapRun({
      ok: true,
      run: {
        run_id: "ginno-runtime.vabc.1",
        sid: "sess-1",
        stage: "build_runtime",
        state: "running",
        detail: "PyInstaller",
        pct: 55,
        log_tail: null,
        ts: 1730700000,
        started_at: 1730699000,
        stages: { quiesce: "ok", unit: "ok", build_runtime: "running" },
      },
    });
    expect(r).not.toBeNull();
    expect(r!.run_id).toBe("ginno-runtime.vabc.1");
    expect(r!.sid).toBe("sess-1");
    expect(r!.startedAt).toBe(1730699000);
    expect(r!.pct).toBe(55);
  });

  it("null run in snapshot yields null", () => {
    expect(normalizeDevSwapRun({ ok: true, run: null })).toBeNull();
  });

  it("rejects missing run_id / unknown stage or state", () => {
    expect(normalizeDevSwapRun({ stage: "unit", state: "running" })).toBeNull();
    expect(normalizeDevSwapRun({ run_id: "x", stage: "nope", state: "running" })).toBeNull();
    expect(normalizeDevSwapRun({ run_id: "x", stage: "unit", state: "weird" })).toBeNull();
  });

  it("accepts all six stages", () => {
    for (const s of DEVSWAP_STAGES) {
      expect(normalizeDevSwapRun({ run_id: "x", stage: s, state: "running" })?.stage).toBe(s);
    }
  });
});

describe("stageCells", () => {
  it("running mid-pipeline: earlier done, self active, later pending", () => {
    const cells = stageCells(normalizeDevSwapRun(RUN)!);
    expect(cells).toEqual(["done", "done", "active", "pending", "pending", "pending"]);
  });

  it("ok at finish: all done", () => {
    const cells = stageCells(
      normalizeDevSwapRun({ ...RUN, stage: "finish", state: "ok", pct: null })!,
    );
    expect(cells.every((c) => c === "done")).toBe(true);
  });

  it("fail marks only the failing stage red, earlier done", () => {
    const cells = stageCells(
      normalizeDevSwapRun({ ...RUN, stage: "preflight", state: "fail" })!,
    );
    expect(cells).toEqual(["done", "done", "done", "fail", "pending", "pending"]);
  });

  it("rolled_back behaves like fail for the timeline", () => {
    const cells = stageCells(
      normalizeDevSwapRun({ ...RUN, stage: "finish", state: "rolled_back" })!,
    );
    expect(cells[5]).toBe("fail");
    expect(cells.slice(0, 5).every((c) => c === "done")).toBe(true);
  });

  it("isDevSwapActive", () => {
    expect(isDevSwapActive(normalizeDevSwapRun(RUN)!)).toBe(true);
    expect(isDevSwapActive(normalizeDevSwapRun({ ...RUN, state: "ok" })!)).toBe(false);
    expect(isDevSwapActive(null)).toBe(false);
  });
});

describe("stats EMA/P90", () => {
  it("first record seeds EMA from the sample; default expectation used before any data", () => {
    const st = memStorage();
    expect(expectationFor("build_runtime", loadStats(st)).ema).toBe(
      DEVSWAP_DEFAULT_SECONDS.build_runtime,
    );
    recordStageSeconds("build_runtime", 300, st);
    const exp = expectationFor("build_runtime", loadStats(st));
    expect(exp.ema).toBe(300);
    // 单样本：无 P90 告警能力
    expect(exp.hasP90).toBe(false);
  });

  it("EMA converges and P90 warning arms after >=2 samples", () => {
    const st = memStorage();
    recordStageSeconds("unit", 90, st);
    recordStageSeconds("unit", 210, st); // ema = 90*0.7 + 210*0.3 = 126
    const exp = expectationFor("unit", loadStats(st));
    expect(exp.ema).toBeCloseTo(126, 5);
    expect(exp.hasP90).toBe(true);
    expect(exp.p90).toBe(210);
  });

  it("save/load roundtrip via storage key", () => {
    const st = memStorage();
    recordStageSeconds("preflight", 25, st);
    const raw = st.dump()["ginno:devswap-stats"];
    expect(raw).toBeTruthy();
    const loaded = loadStats(st);
    expect(loaded.preflight?.ema).toBe(25);
  });

  it("corrupt JSON degrades to empty stats, not a throw", () => {
    const st = memStorage();
    st.setItem("ginno:devswap-stats", "{not json");
    expect(loadStats(st)).toEqual({});
  });

  it("no storage at all: stats no-op but expectations fall back to defaults", () => {
    recordStageSeconds("unit", 100, null);
    expect(expectationFor("unit", loadStats(null)).ema).toBe(
      DEVSWAP_DEFAULT_SECONDS.unit,
    );
  });

  it("percentile90 picks the ceil(0.9n)-th sorted sample", () => {
    expect(percentile90([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])).toBe(9);
    expect(percentile90([5, 1, 3])).toBe(5);
    expect(percentile90([])).toBe(0);
  });
});

describe("wait line data", () => {
  it("formats bar/clock and flags slow past P90", () => {
    const exp = { ema: 240, p90: 300, hasP90: true };
    const d = waitLineData(121, 55, exp);
    expect(d.bar).toBe("▓▓▓░░");
    expect(d.elapsedLabel).toBe("2:01");
    expect(d.etaLabel).toBe("1:59");
    expect(d.pctLabel).toBe("55%");
    expect(d.slow).toBe(false);
    expect(waitLineData(301, 55, exp).slow).toBe(true);
  });

  it("missing pct yields empty bar/pct", () => {
    const d = waitLineData(5, null, { ema: 10, p90: 10, hasP90: false });
    expect(d.bar).toBe("");
    expect(d.pctLabel).toBe("");
    expect(d.slow).toBe(false); // 无 P90 样本不告警
  });

  it("progressBar bounds", () => {
    expect(progressBar(0)).toBe("░░░░░");
    expect(progressBar(100)).toBe("▓▓▓▓▓");
    expect(progressBar(150)).toBe("▓▓▓▓▓");
    expect(progressBar(undefined)).toBe("");
  });

  it("fmtClock", () => {
    expect(fmtClock(0)).toBe("0:00");
    expect(fmtClock(65)).toBe("1:05");
    expect(fmtClock(-3)).toBe("0:00");
  });
});
