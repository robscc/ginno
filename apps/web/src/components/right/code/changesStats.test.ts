import { describe, expect, it } from "vitest";
import { lineDiffCounts } from "./changesStats";

describe("lineDiffCounts", () => {
  it("returns zeros for identical text", () => {
    expect(lineDiffCounts("a\nb\nc", "a\nb\nc")).toEqual({ add: 0, del: 0 });
  });

  it("counts pure additions after stripping the common prefix/suffix", () => {
    expect(lineDiffCounts("a\nb", "a\nx\nb")).toEqual({ add: 1, del: 0 });
  });

  it("counts pure deletions", () => {
    expect(lineDiffCounts("a\nb\nc", "a\nc")).toEqual({ add: 0, del: 1 });
  });

  it("renders an interior edit as replace (del+add)", () => {
    expect(lineDiffCounts("a\nold\nz", "a\nnew\nz")).toEqual({ add: 1, del: 1 });
  });
});
