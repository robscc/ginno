import { beforeEach, describe, expect, it, vi } from "vitest";
import { getHistoryTool } from "@/lib/api/sessions";
import { loadHistoryTool } from "./historyTool";

vi.mock("@/lib/api/sessions", () => ({ getHistoryTool: vi.fn() }));
const getTool = vi.mocked(getHistoryTool);

describe("lazy history tool detail", () => {
  beforeEach(() => getTool.mockReset());

  it("shares concurrent reads by session and record", async () => {
    const detail = { content: "full saved output", diff: null };
    getTool.mockResolvedValue(detail);
    const first = loadHistoryTool("session-a", "tool");
    expect(loadHistoryTool("session-a", "tool")).toBe(first);
    const other = loadHistoryTool("session-b", "tool");
    expect(other).not.toBe(first);
    expect(getTool.mock.calls).toEqual([["session-a", "tool"], ["session-b", "tool"]]);
    await expect(Promise.all([first, other])).resolves.toEqual([detail, detail]);
  });

  it("lets the user retry a rejected shared request", async () => {
    getTool.mockRejectedValueOnce(new Error("unavailable"));
    const first = loadHistoryTool("retry-session", "tool");
    expect(loadHistoryTool("retry-session", "tool")).toBe(first);
    await expect(first).rejects.toThrow("unavailable");
    getTool.mockResolvedValueOnce({ content: "result", diff: null });
    await expect(loadHistoryTool("retry-session", "tool")).resolves.toHaveProperty("content", "result");
    expect(getTool).toHaveBeenCalledTimes(2);
  });
});
