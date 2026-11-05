import { describe, expect, it } from "vitest";

import { reloadNeedsConfirm } from "./reloadGuard";

describe("reloadNeedsConfirm (issue #8)", () => {
  it("clean tab reloads without confirm", () => {
    expect(reloadNeedsConfirm({ dirty: false, conflict: null })).toBe(false);
  });

  it("dirty tab requires confirm", () => {
    expect(reloadNeedsConfirm({ dirty: true, conflict: null })).toBe(true);
  });

  it("conflicted tab requires confirm even when not dirty", () => {
    expect(
      reloadNeedsConfirm({
        dirty: false,
        conflict: { version: "v3", diskText: "x", mine: "y" },
      }),
    ).toBe(true);
  });

  it("missing meta (tab gone) never blocks", () => {
    expect(reloadNeedsConfirm(undefined)).toBe(false);
    expect(reloadNeedsConfirm(null)).toBe(false);
  });
});
