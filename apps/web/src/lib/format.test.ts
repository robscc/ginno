import { describe, expect, it } from "vitest";
import { fmtTokens } from "./format";

describe("fmtTokens", () => {
  it("passes small numbers through (rounded)", () => {
    expect(fmtTokens(0)).toBe("0");
    expect(fmtTokens(950)).toBe("950");
    expect(fmtTokens(999)).toBe("999");
  });

  it("formats thousands / millions / billions with one decimal", () => {
    expect(fmtTokens(1000)).toBe("1k");
    expect(fmtTokens(12_400)).toBe("12.4k");
    expect(fmtTokens(999_999)).toBe("1000k");
    expect(fmtTokens(1_000_000)).toBe("1M");
    expect(fmtTokens(1_250_000)).toBe("1.25M");
    expect(fmtTokens(150_000_000)).toBe("150M");
    expect(fmtTokens(3_000_000_000)).toBe("3B");
    expect(fmtTokens(1_500_000_000)).toBe("1.5B");
  });

  it("trims a trailing .0", () => {
    expect(fmtTokens(12_000)).toBe("12k");
    expect(fmtTokens(2_000_000)).toBe("2M");
  });

  it("keeps locale-independent digits (no thousands separators)", () => {
    expect(fmtTokens(950)).not.toContain(",");
  });

  it("handles negatives below the first scale", () => {
    expect(fmtTokens(-500)).toBe("-500");
  });

  it("returns \"0\" for null/undefined/non-finite input", () => {
    expect(fmtTokens(undefined)).toBe("0");
    expect(fmtTokens(null)).toBe("0");
    expect(fmtTokens(Number.NaN)).toBe("0");
    expect(fmtTokens(Number.POSITIVE_INFINITY)).toBe("0");
  });
});
