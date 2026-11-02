import { describe, expect, it } from "vitest";
import { cn } from "./utils";

describe("cn (clsx + tailwind-merge)", () => {
  it("joins classes", () => {
    expect(cn("a", "b")).toBe("a b");
  });

  it("lets later utilities win on conflicts", () => {
    expect(cn("p-2", "p-4")).toBe("p-4");
    expect(cn("text-red-500", "text-blue-500")).toBe("text-blue-500");
  });

  it("skips falsy inputs", () => {
    expect(cn("px-2", false && "py-2", undefined, null)).toBe("px-2");
  });

  it("returns empty string for no input", () => {
    expect(cn()).toBe("");
  });
});
