import { describe, expect, it } from "vitest";
import { parsePlanUpdate } from "./planUpdateParse";

describe("parsePlanUpdate（清单更新卡：只留条目状态变化）", () => {
  it("折出条目状态变化，丢掉给模型看的抬头，多行 detail 归属上一条", () => {
    const text = [
      "[plan update] Task list status changed:",
      "- [done] it_4 前端尾项已完成，可以进入联调。",
      "### 证据",
      "",
      "1. `node --check app.js` 通过",
      "- [failed] it_6 ESM 解析失败",
      "plan pl_411d2ea97 COMPLETED: 1 done, 1 failed",
    ].join("\n");
    expect(parsePlanUpdate(text)).toEqual({
      items: [
        {
          id: "it_4",
          status: "done",
          detail: "前端尾项已完成，可以进入联调。\n### 证据\n\n1. `node --check app.js` 通过",
        },
        { id: "it_6", status: "failed", detail: "ESM 解析失败" },
      ],
      completed: { planId: "pl_411d2ea97", done: 1, failed: 1 },
      notes: [],
    });
  });

  it("未知形状保留为普通行（旧数据与非标准文本不丢）", () => {
    expect(parsePlanUpdate("Live system plan receipt").notes).toEqual(["Live system plan receipt"]);
    expect(parsePlanUpdate("")).toEqual({ items: [], completed: null, notes: [] });
    expect(parsePlanUpdate("[plan update] Plan receipt sentinel")).toEqual({
      items: [], completed: null, notes: ["Plan receipt sentinel"],
    });
  });
});
