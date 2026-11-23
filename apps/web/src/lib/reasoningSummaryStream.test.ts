import { expect, it, vi } from "vitest";
import { trackReasoningSummaries } from "./reasoningSummaryStream";

it("requests one recovery at a time and drops replies after the turn ends", () => {
  const send = vi.fn();
  const accept = trackReasoningSummaries(send);
  const delta = (baseRevision: number, revision: number) =>
    ({ event: "reasoning.summary", summaryDelta: { requestId: "r", baseRevision, revision } });
  const recovery = (revision: number) =>
    ({ event: "reasoning.summary", recovery: true, summary: { requestId: "r", revision } });

  expect(accept(delta(0, 1))).toBe(true);
  expect(accept(delta(2, 3))).toBe(false);
  expect(accept(delta(3, 4))).toBe(false);
  expect(send).toHaveBeenCalledTimes(1);
  expect(send).toHaveBeenCalledWith({ type: "reasoning_summary_state", requestId: "r" });
  expect(accept(recovery(3))).toBe(true);
  expect(send).toHaveBeenCalledTimes(2);
  expect(accept(recovery(4))).toBe(true);
  expect(accept(delta(4, 5))).toBe(true);

  expect(accept(delta(6, 7))).toBe(false);
  accept({ event: "message.end" });
  expect(accept(recovery(7))).toBe(false);
});
