import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReplyMergeWindow, type MergeItem } from "./notifyHub";
import { fromWire, inDndWindow } from "./notifyPrefs";

describe("ReplyMergeWindow", () => {
  let flushed: Array<[string, MergeItem[]]>;
  let clock: { t: number; timers: Array<{ at: number; fn: () => void }> };
  let w: ReplyMergeWindow;

  beforeEach(() => {
    flushed = [];
    clock = { t: 0, timers: [] };
    w = new ReplyMergeWindow({
      windowMs: 30_000,
      now: () => clock.t,
      schedule: (fn, ms) => {
        const entry = { at: clock.t + ms, fn };
        clock.timers.push(entry);
        return () => {
          const i = clock.timers.indexOf(entry);
          if (i >= 0) clock.timers.splice(i, 1);
        };
      },
      onFlush: (key, items) => flushed.push([key, items]),
    });
  });

  const advance = (ms: number) => {
    clock.t += ms;
    // fire due timers (sorted copy — firing may not enqueue more here)
    for (const e of [...clock.timers].sort((a, b) => a.at - b.at)) {
      if (e.at <= clock.t) e.fn();
    }
  };

  it("单条：窗口到点后原样 flush", () => {
    w.push("s1", { title: "A", body: "done" });
    expect(w.pending("s1")).toBe(1);
    advance(31_000);
    expect(flushed).toEqual([["s1", [{ title: "A", body: "done" }]]]);
    expect(w.pending("s1")).toBe(0);
  });

  it("窗口内多条合并 flush（不同 key 互不混）", () => {
    w.push("s1", { title: "A", body: "1" });
    advance(10_000);
    w.push("s1", { title: "A", body: "2" });
    w.push("s2", { title: "B", body: "x" });
    advance(21_000); // s1 窗口(0→30s)到期；s2 窗口(10s→40s)未到
    expect(flushed).toEqual([["s1", [{ title: "A", body: "1" }, { title: "A", body: "2" }]]]);
    expect(w.pending("s2")).toBe(1);
    advance(10_000);
    expect(flushed[1]).toEqual(["s2", [{ title: "B", body: "x" }]]);
  });

  it("flush 后新窗口重新起算", () => {
    w.push("s1", { title: "A", body: "1" });
    advance(30_000);
    w.push("s1", { title: "A", body: "2" });
    expect(w.pending("s1")).toBe(1);
    advance(30_000);
    expect(flushed.map(([k, items]) => [k, items.length])).toEqual([["s1", 1], ["s1", 1]]);
  });

  it("cancel 丢弃不 flush", () => {
    w.push("s1", { title: "A", body: "1" });
    w.cancel("s1");
    advance(60_000);
    expect(flushed).toEqual([]);
  });
});

describe("inDndWindow", () => {
  it("默认关闭恒 false", () => {
    expect(inDndWindow({ enabled: false, from: "22:00", to: "08:00" }, 300)).toBe(false);
  });
  it("跨午夜窗口：23:59 与 07:00 命中，12:00 不命中", () => {
    const dnd = { enabled: true, from: "22:00", to: "08:00" };
    expect(inDndWindow(dnd, 23 * 60 + 59)).toBe(true);
    expect(inDndWindow(dnd, 7 * 60)).toBe(true);
    expect(inDndWindow(dnd, 12 * 60)).toBe(false);
    expect(inDndWindow(dnd, 22 * 60)).toBe(true);
    expect(inDndWindow(dnd, 8 * 60)).toBe(false);
  });
  it("当日窗口：14:00-18:00", () => {
    const dnd = { enabled: true, from: "14:00", to: "18:00" };
    expect(inDndWindow(dnd, 15 * 60)).toBe(true);
    expect(inDndWindow(dnd, 13 * 60 + 59)).toBe(false);
    expect(inDndWindow(dnd, 18 * 60)).toBe(false);
  });
});

describe("fromWire 迁移", () => {
  it("老格式（V1 三键）补默认：askUser/failure/reply 开、workflow/subagent 默认关（2026-11-10 收敛）", () => {
    const p = fromWire({ enabled: false, sound: true, sound_name: "Glass" });
    expect(p.enabled).toBe(false);
    expect(p.kinds.reply).toBe(true);
    expect(p.kinds.workflow).toBe(false);
    expect(p.kinds.subagent).toBe(false);
    expect(p.privacy).toBe("full");
    expect(p.dnd.enabled).toBe(false);
  });
});

import { KindRateGate } from "./notifyHub";

describe("KindRateGate（跨会话 reply 冷却）", () => {
  let t = 0;
  beforeEach(() => (t = 0));

  it("窗口内吞并计数，放行时带回 suppressed 并清零", () => {
    const g = new KindRateGate(60_000, () => t);
    expect(g.hit("reply")).toEqual({ ok: true, suppressed: 0 });
    t = 10_000;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 1 });
    t = 30_000;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 2 });
    t = 61_000;
    expect(g.hit("reply")).toEqual({ ok: true, suppressed: 2 });
    t = 61_001;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 1 });
  });

  it("各 kind 独立冷却", () => {
    const g = new KindRateGate(60_000, () => t);
    g.hit("reply");
    t = 1_000;
    expect(g.hit("subagent").ok).toBe(true);
    expect(g.hit("reply").ok).toBe(false);
  });
});
