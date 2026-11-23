import { expect, it } from "vitest";
import { applyBlock } from "./streamCore";
import type { Block } from "./blocks/types";

it("keeps one summary across tools and ignores duplicate or older reconnect snapshots", () => {
  const first = { id: "request-a/0/0", text: "Check inputs" };
  const next = { id: "request-b/0/0", text: "Verify result" };
  const apply = (blocks: Block[], revision: number, parts: typeof first[]) => applyBlock(blocks, {
    event: "reasoning.summary", summary: { revision, parts, receiving: true },
  });
  const before = apply([], 1, [first]);
  const withTool = applyBlock(before, { event: "tool.start", id: "t", name: "bash" });
  const current = apply(withTool, 3, [first, next]);
  expect(current.map(b => b.kind)).toEqual(["reasoning_summary", "tool"]);
  expect(current[0]).toMatchObject({ parts: [first, next] });
  expect(apply(current, 2, [first])).toBe(current);
  expect(apply(current, 3, [first, next])).toBe(current);
});
