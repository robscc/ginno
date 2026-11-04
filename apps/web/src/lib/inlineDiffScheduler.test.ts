import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { InlineDiffScheduler } from "./inlineDiffScheduler";

describe("InlineDiffScheduler", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("fires once after the delay with the newest payload", () => {
    const onFire = vi.fn();
    const s = new InlineDiffScheduler<string>(500, onFire);
    s.schedule("/a", "v1", "old");
    s.schedule("/a", "v2", "new");
    s.schedule("/a", "v3", "newest");
    vi.advanceTimersByTime(499);
    expect(onFire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onFire).toHaveBeenCalledTimes(1);
    expect(onFire).toHaveBeenCalledWith({ path: "/a", version: "v3", data: "newest" });
  });

  it("coalesces events arriving inside the window (timer reset)", () => {
    const onFire = vi.fn();
    const s = new InlineDiffScheduler<string>(500, onFire);
    s.schedule("/a", "v1", "x");
    vi.advanceTimersByTime(300);
    s.schedule("/a", "v2", "y");
    vi.advanceTimersByTime(300); // only 300ms since the reset
    expect(onFire).not.toHaveBeenCalled();
    vi.advanceTimersByTime(200);
    expect(onFire).toHaveBeenCalledTimes(1);
    expect(onFire.mock.calls[0][0].version).toBe("v2");
  });

  it("keeps different paths independent", () => {
    const onFire = vi.fn();
    const s = new InlineDiffScheduler<string>(500, onFire);
    s.schedule("/a", "v1", "a");
    s.schedule("/b", "v1", "b");
    vi.advanceTimersByTime(500);
    expect(onFire).toHaveBeenCalledTimes(2);
  });

  it("cancel drops the pending change; dispose clears everything", () => {
    const onFire = vi.fn();
    const s = new InlineDiffScheduler<string>(500, onFire);
    s.schedule("/a", "v1", "x");
    s.cancel("/a");
    vi.advanceTimersByTime(1000);
    expect(onFire).not.toHaveBeenCalled();
    s.schedule("/b", "v1", "y");
    s.dispose();
    vi.advanceTimersByTime(1000);
    expect(onFire).not.toHaveBeenCalled();
  });
});
