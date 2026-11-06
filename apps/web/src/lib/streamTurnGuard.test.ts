import { describe, expect, it, beforeEach } from "vitest";
import {
  decideTurnAdoption,
  mismatchReportBudget,
  resetDiagState,
} from "./streamTurnGuard";

describe("decideTurnAdoption —— turn 漂移认领", () => {
  beforeEach(resetDiagState);

  it("孤儿流首帧认领（既有语义保持）", () => {
    expect(
      decideTurnAdoption({ bubbleTurn: "old", evTurn: "new", orphan: true, adopted: false }),
    ).toBe("adopt");
    expect(
      decideTurnAdoption({ bubbleTurn: "old", evTurn: "new", orphan: true, adopted: true }),
    ).toBe("no"); // 只认领一次
  });

  it("【回归·2026-11-06 16aef0a3 实录】连接中 turn 漂移也认领：live bubble 的 turnId 跟随服务端", () => {
    // sidecar 重启/resume 生成新 turn_id，前端旧 bubble 未重建——此前
    // orphan=false 不认领 → 17 分钟 10,051 条 mismatch（每帧上报）。
    expect(
      decideTurnAdoption({ bubbleTurn: "A", evTurn: "B", orphan: false, adopted: false }),
    ).toBe("adopt");
    // 同一漂移对不重复认领（下一帧 bubble 已是 B）
    expect(
      decideTurnAdoption({ bubbleTurn: "B", evTurn: "B", orphan: false, adopted: true }),
    ).toBe("no");
  });

  it("无 evTurn 或一致：不动", () => {
    expect(decideTurnAdoption({ bubbleTurn: "A", evTurn: undefined, orphan: false, adopted: false })).toBe("no");
    expect(decideTurnAdoption({ bubbleTurn: "A", evTurn: "A", orphan: false, adopted: false })).toBe("no");
  });
});

describe("mismatchReportBudget —— 诊断上报限频", () => {
  beforeEach(resetDiagState);

  it("同一 (session, bubble, ev) 漂移对只报一次", () => {
    const key = { sid: "s1", bubble: "A", ev: "B" };
    expect(mismatchReportBudget(key)).toBe(true); // first
    expect(mismatchReportBudget(key)).toBe(false);
    expect(mismatchReportBudget(key)).toBe(false);
  });

  it("不同漂移对各报一次（并发场景不被合并误伤）", () => {
    expect(mismatchReportBudget({ sid: "s1", bubble: "A", ev: "B" })).toBe(true);
    expect(mismatchReportBudget({ sid: "s1", bubble: "B", ev: "C" })).toBe(true);
    expect(mismatchReportBudget({ sid: "s2", bubble: "A", ev: "B" })).toBe(true);
  });

  it("【洪水回归】一万帧同一漂移对 → 恰好 1 次上报（10,051 条实录不再出现）", () => {
    let reported = 0;
    for (let i = 0; i < 10_000; i++) {
      if (mismatchReportBudget({ sid: "s", bubble: "A", ev: "B" })) reported++;
    }
    expect(reported).toBe(1);
  });
});
