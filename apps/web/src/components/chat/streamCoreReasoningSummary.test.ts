import { expect, it } from "vitest";
import { applyBlock } from "./streamCore";
import type { Block } from "./blocks/types";

function snapshot(requestId: string, revision: number, text: string, receiving = true) {
  return { requestId, revision, parts: [{ id: `${requestId}/0/0`, text }], receiving };
}

function applySummary(blocks: Block[], summary: ReturnType<typeof snapshot>) {
  return applyBlock(blocks, { event: "reasoning.summary", summary });
}

it("keeps a separate card at each request and does not reopen an older card on replay", () => {
  const first = snapshot("request-a", 2, "Check inputs", false);
  const second = snapshot("request-b", 3, "Verify result");
  const before = applySummary([], first);
  const withTool = applyBlock(before, { event: "tool.start", id: "t", name: "bash", requestId: first.requestId });
  const current = applySummary(withTool, second);
  expect(current.map(b => b.kind)).toEqual(["reasoning_summary", "tool", "reasoning_summary"]);
  expect(current[0]).toBe(before[0]);
  expect(current[2]).toMatchObject(second);
  expect(applySummary(current, snapshot(first.requestId, 1, "Partial"))).toBe(current);
  expect(applySummary(current, second)).toBe(current);

  const finished = applySummary(current, snapshot(second.requestId, 4, "Verify final result", false));
  expect(finished.slice(0, 2)).toEqual(current.slice(0, 2));
  expect(finished[2]).toMatchObject({ receiving: false, parts: [{ text: "Verify final result" }] });
});

it("restores missing summaries before their own surviving tool or text blocks", () => {
  const first = snapshot("request-a", 1, "Check inputs", false);
  const second = snapshot("request-b", 2, "Verify result", false);
  const tool = applyBlock([], { event: "tool.end", id: "t", name: "bash", content: "ok", requestId: first.requestId });
  const body = applyBlock(tool, { event: "token.delta", content: "Done", requestId: second.requestId });
  const restored = applyBlock(body, { event: "turn.state", reasoning_summaries: [first, second] });
  expect(restored.map(b => b.kind)).toEqual(["reasoning_summary", "tool", "reasoning_summary", "text"]);
  expect(restored[0]).toMatchObject(first);
  expect(restored[2]).toMatchObject(second);
  expect(applyBlock(restored, { event: "message.end", reasoning_summaries: [first, second] })).toBe(restored);

  // A request that was entirely missed still belongs before the next known card.
  const missed = snapshot("request-missed", 3, "While offline", false);
  const next = snapshot("request-next", 4, "After reconnect", false);
  const tail = applyBlock(restored, { event: "token.delta", content: "Next answer", requestId: next.requestId });
  const complete = applyBlock(tail, { event: "message.end", reasoning_summaries: [first, second, missed, next] });
  expect(complete.slice(4)).toEqual([
    { kind: "reasoning_summary", ...missed },
    { kind: "reasoning_summary", ...next },
    { kind: "text", requestId: next.requestId, text: "Next answer" },
  ]);
});

it("appends each delta once and recovers a missing revision before continuing", () => {
  const first = snapshot("request", 1, "Check ");
  const sendDelta = (blocks: Block[], baseRevision: number, summary: ReturnType<typeof snapshot>) =>
    applyBlock(blocks, { event: "reasoning.summary", summaryDelta: { ...summary, baseRevision } });
  const current = sendDelta([], 0, first);
  const late = snapshot(first.requestId, 3, "carefully");
  expect(sendDelta(current, 2, late)).toBe(current);
  const recovered = applySummary(current, snapshot(first.requestId, 2, "Check inputs "));
  const appended = sendDelta(recovered, 2, late);
  const final = sendDelta(appended, 3, { ...late, revision: 4, parts: [], receiving: false });
  expect(sendDelta(final, 2, late)).toBe(final);
  expect(final[0]).toMatchObject({ receiving: false, parts: [{ text: "Check inputs carefully" }] });
});
