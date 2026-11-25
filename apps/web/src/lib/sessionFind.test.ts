import { describe, expect, it } from "vitest";
import { foldReportRows } from "@/components/chat/blocks/reportFold";
import type { ChatMsg } from "@/components/chat/streamCore";
import {
  buildFindIndex,
  searchableBlockText,
  searchFindIndex,
  transcriptRowKeys,
} from "./sessionFind";

function index(messages: ChatMsg[]) {
  return buildFindIndex(messages, {
    folded: foldReportRows(messages, { attention: () => null }),
    rowKeys: transcriptRowKeys(messages),
  }, (block) => block.kind === "plan_update" || block.kind === "context"
    ? block.text : searchableBlockText(block));
}

describe("sessionFind", () => {
  it("extracts ordinary text, tool output and skill names, skipping pending tools and chrome", () => {
    expect(index([{ id: "message", role: "assistant", blocks: [
      { kind: "text", text: "正文" },
      { kind: "thinking", text: "推理" },
      { kind: "tool", id: "one", name: "bash", argsPreview: "ls", content: "out", pending: false },
      { kind: "tool", id: "two", name: "bash", content: "…", pending: true },
      { kind: "context", text: "系统行" },
      { kind: "file", name: "报告.xlsx" },
      { kind: "image", url: "http://x" },
    ] }]).map(({ text, blockIdx }) => [text, blockIdx])).toEqual([
      ["正文", 0], ["推理", 1], ["bash\nls\nout", 2], ["系统行", 4], ["报告.xlsx", 5],
    ]);
  });

  it("keeps the source address when a replayed plan with a null id moves to a new host", () => {
    const plan = { kind: "plan_update", planId: "sample", text: "needle" } as const;
    const live: ChatMsg[] = [
      { id: "live-host", role: "assistant", blocks: [{ kind: "text", text: "live" }] },
      { id: "live-row", role: "system", blocks: [plan] },
    ];
    const replay: ChatMsg[] = [
      { id: "history-host", role: "assistant", blocks: [{ kind: "text", text: "history" }] },
      { id: null as unknown as string, role: "assistant", blocks: [plan, { kind: "text", text: "following" }] },
    ];
    const [before] = searchFindIndex(index(live), "needle");
    const [after] = searchFindIndex(index(replay), "needle");
    expect(after.sourceKey).toBe(before.sourceKey);
    expect(after.rowKey).toBe(transcriptRowKeys(replay)[0]);
    expect(after.rowKey).not.toBe(before.rowKey);
    expect(after.blockIdx).toBe(1);
    expect(transcriptRowKeys(replay)[1]).toContain("legacy");
  });

  it("orders repeated literal occurrences and distinguishes identical source blocks", () => {
    const block = { kind: "text", text: "Read [config], read [config]" } as const;
    const indexed = index([{ id: "one", role: "assistant", blocks: [block, block] }]);
    const hits = searchFindIndex(indexed, " READ [CONFIG] ");
    expect(hits.map(({ blockIdx, occurrence }) => [blockIdx, occurrence])).toEqual([[0, 0], [0, 1], [1, 0], [1, 1]]);
    expect(hits[0].sourceKey).not.toBe(hits[2].sourceKey);
    expect(searchFindIndex(indexed, "  ")).toEqual([]);
  });
});

it("keeps the original notice ordinal after a sibling is dismissed", () => {
  const first = { kind: "context", text: "One notice" } as const;
  const second = { kind: "context", text: "Another notice" } as const;
  const original: ChatMsg[] = [{ id: "notices", role: "system", blocks: [first, second] }];
  const visible: ChatMsg[] = [{ ...original[0], blocks: [second] }];
  const [before] = searchFindIndex(index(original), "Another");
  const [after] = searchFindIndex(buildFindIndex(visible, {
    folded: foldReportRows(visible, { attention: () => null }), rowKeys: transcriptRowKeys(visible),
  }, () => second.text, original), "Another");
  expect(after.sourceKey).toBe(before.sourceKey);
  expect(after.blockIdx).toBe(0);
});

it("keeps folded reasoning, tool diff and question text addressable", () => {
  const indexed = index([{ id: "folded", role: "assistant", blocks: [
    { kind: "thinking", text: "Read config" },
    { kind: "reasoning_summary", parts: [{ id: "part-1", text: "Checked config" }] },
    { kind: "tool", name: "edit_file", pending: false, content: "Updated", diff: {
      op: "edit", path: "config.ini", text: "+ enable_preview", added: 1, removed: 0, truncated: false,
    } },
    { kind: "question", question: "Which environment?", options: ["Staging", "Production"], allowFreeText: false, status: "answered", answer: "Staging" },
  ] }]);
  expect(searchFindIndex(indexed, "config").map(({ blockIdx }) => blockIdx)).toEqual([0, 1, 2]);
  expect(searchFindIndex(indexed, "enable_preview").map(({ blockIdx }) => blockIdx)).toEqual([2]);
  expect(searchFindIndex(indexed, "Staging").map(({ blockIdx }) => blockIdx)).toEqual([3, 3]);
});

it("keeps an unchanged text block's source identity across unrelated appends", () => {
  const text = { kind: "text", text: "stable data" } as const;
  const original: ChatMsg = { id: "live", role: "assistant", blocks: [text] };
  const updated: ChatMsg = { ...original, blocks: [{ kind: "turn_meta", elapsedS: 1 }, text] };
  expect(index([updated])[0]).toMatchObject({ sourceKey: index([original])[0].sourceKey, blockIdx: 1 });
});
