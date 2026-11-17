import { describe, expect, it } from "vitest";
import {
  SLICE_INITIAL_BLOCKS,
  SLICE_MAX_MSGS,
  SLICE_MIN_RENDERED,
  SLICE_RESET_JUMP,
  SLICE_STEP_BLOCKS,
  clampSkip,
  sliceResetNeeded,
  sliceStart,
} from "./transcriptSlices";

describe("sliceStart — block 预算切片边界", () => {
  it("尾部起算，累计到预算即停", () => {
    const counts = [10, 10, 10, 10, 10]; // 50 blocks
    // 预算 25：挂尾 3 条（30 > 25 时第 3 条已挂——第 2 条后 20，加第 3 条 30>25 停）
    expect(sliceStart(counts, 5, 25)).toBe(3); // 挂 idx 3,4（20 blocks）
    expect(sliceStart(counts, 5, 30)).toBe(2); // 挂 idx 2,3,4（30 blocks）
    expect(sliceStart(counts, 5, 100)).toBe(0); // 预算富余 → 全挂
  });

  it("单条超预算也至少挂 1 条（消息内部不细分）", () => {
    const counts = [5, 500, 5];
    expect(sliceStart(counts, 3, 10)).toBe(2); // 尾条 5 ≤ 10，再往前 500 超 → 停
    expect(sliceStart(counts, 2, 10)).toBe(1); // 500 超预算也必须整条挂
  });

  it("maxMsgs 上限：超轻消息不切碎", () => {
    const counts = Array(40).fill(1);
    expect(sliceStart(counts, 40, 1000, 16)).toBe(24);
    expect(sliceStart(counts, 40, 1000)).toBe(40 - SLICE_MAX_MSGS);
  });

  it("边界：end=0 / 空数组 / end 越界", () => {
    expect(sliceStart([], 0, 100)).toBe(0);
    expect(sliceStart([3, 3], 0, 100)).toBe(0);
    expect(sliceStart([3, 3], 99, 100)).toBe(0); // end 夹到 length
  });

  it("逐片推进收敛到 0（模拟 idle 循环）", () => {
    // 重会话剖面：80 条 × ~21 blocks ≈ 1695
    const counts = Array(80).fill(21);
    let skip = sliceStart(counts, 80, SLICE_INITIAL_BLOCKS);
    expect(skip).toBeGreaterThan(0);
    expect(80 - skip).toBeGreaterThanOrEqual(1);
    let guard = 0;
    while (skip > 0 && guard++ < 200) {
      const next = sliceStart(counts, skip, SLICE_STEP_BLOCKS);
      expect(next).toBeLessThan(skip); // 每片必须前进，否则死循环
      skip = next;
    }
    expect(skip).toBe(0);
    expect(guard).toBeLessThan(40); // 1695/120 ≈ 15 片量级
  });
});

describe("clampSkip — 首帧保底与残留夹紧", () => {
  it("正常切片区间不受影响", () => {
    expect(clampSkip(60, 80)).toBe(60);
    expect(clampSkip(0, 80)).toBe(0);
  });

  it("旧会话残留 skip ≥ 新窗口长度 → 仍保底尾部 2 条", () => {
    expect(clampSkip(184, 75)).toBe(75 - SLICE_MIN_RENDERED);
    expect(clampSkip(80, 80)).toBe(80 - SLICE_MIN_RENDERED);
  });

  it("小窗口（≤ 保底条数）全量渲染", () => {
    expect(clampSkip(10, 2)).toBe(0);
    expect(clampSkip(2, 2)).toBe(0);
    expect(clampSkip(0, 0)).toBe(0);
  });
});

describe("sliceResetNeeded — 重置判定", () => {
  it("会话切换必重置", () => {
    expect(sliceResetNeeded({ sid: "a", len: 80 }, "b", 80)).toBe(true);
    expect(sliceResetNeeded({ sid: undefined, len: 0 }, "a", 0)).toBe(true);
  });

  it("整批历史到达（0→80）重置", () => {
    expect(sliceResetNeeded({ sid: "a", len: 0 }, "a", 80)).toBe(true);
    expect(sliceResetNeeded({ sid: "a", len: 2 }, "a", 2 + SLICE_RESET_JUMP + 1)).toBe(true);
  });

  it("流式追加 / 小幅替换不重置", () => {
    expect(sliceResetNeeded({ sid: "a", len: 80 }, "a", 82)).toBe(false);
    expect(sliceResetNeeded({ sid: "a", len: 80 }, "a", 80)).toBe(false);
    expect(sliceResetNeeded({ sid: "a", len: 80 }, "a", 78)).toBe(false); // 缩水
    expect(sliceResetNeeded({ sid: "a", len: 80 }, "a", 80 + SLICE_RESET_JUMP)).toBe(false); // 恰好阈值不触发
  });
});