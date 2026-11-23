import { describe, expect, it } from "vitest";
import { foldTaskEventBlocks } from "./taskEventParse";

// 历史重放折卡："[task finished]…" 注入带 → 极薄 task_event + tail 保留。
// 样例取自 runtime tasks/inject.py STATUS_TEXT 的真实格式（2026-11-07 会话
// dev-swap-retry-120s 任务的 #keep 消息原文）。
describe("foldTaskEventBlocks", () => {
  it("finished 带解析出 taskId/exit/dur，tail 作为可展开日志保留", () => {
    const src = [
      {
        kind: "text" as const,
        text: '[task finished] "dev-swap-retry-120s" (t_1218152f) exit 2 · ran 2m 00s\n--- tail ---\n{"ok":true}\n--- end ---',
      },
    ];
    const out = foldTaskEventBlocks(src);
    expect(out).toHaveLength(1);
    const tev = out[0] as { kind: string; taskId: string; ev: string; exit: number; dur: string };
    expect(tev.kind).toBe("task_event");
    expect(tev.taskId).toBe("t_1218152f");
    expect(tev.ev).toBe("finished");
    expect(tev.exit).toBe(2);
    expect(tev.dur).toBe("2m 00s");
    expect(out[0]).toMatchObject({ log: expect.stringContaining('{"ok":true}') });
  });

  it("failed to start 带（无 exit/dur）折为 failed_to_start", () => {
    const out = foldTaskEventBlocks([
      { kind: "text" as const, text: '[task failed to start] "probe" (t_ab12cd34) spawn error' },
    ]);
    const tev = out[0] as { kind: string; ev: string; taskId: string };
    expect(out).toHaveLength(1);
    expect(tev.kind).toBe("task_event");
    expect(tev.ev).toBe("failed_to_start");
    expect(tev.taskId).toBe("t_ab12cd34");
  });

  it("普通文本与非 text block 原样透传", () => {
    const src = [
      { kind: "text" as const, text: "普通消息，含 [task finished] 但不在行首" },
      { kind: "task_event" as const, taskId: "t_x", ev: "finished" },
    ];
    expect(foldTaskEventBlocks(src)).toEqual(src);
  });

  it("无尾行的注入带不产生空 text block", () => {
    const out = foldTaskEventBlocks([
      { kind: "text" as const, text: '[task finished] "x" (t_9f8e7d6c) exit 0 · ran 3s' },
    ]);
    expect(out).toHaveLength(1);
  });

  it("preserves the complete legacy lastwords envelope for LastwordsCard", () => {
    const src = [{ kind: "text" as const, text: "[lastwords] Recovery\n\n原始请求：修复构建\n最后进展：跑到单测阶段" }];
    expect(foldTaskEventBlocks(src)).toEqual(src);
  });

  it.each([
    ["timeout", "timeout"], ["stopped by user", "stopped"],
    ["interrupted by app quit", "interrupted"], ["watch", "watch"],
    ["watch muted", "watch_muted"], ["healthy", "healthy"],
  ])("folds the legacy %s event", (header, ev) => {
    const text = `[task ${header}] "run \"build\"" (t_legacy) after 2m 05s`;
    expect(foldTaskEventBlocks([{ kind: "text", text }])).toMatchObject([
      { kind: "task_event", taskId: "t_legacy", ev, title: 'run "build"' },
    ]);
  });

  it("recovers every legacy batched event and preserves unmatched text", () => {
    const text = '[task timeout] "first" (t_first) after 1s — killed\n--- tail ---\nfirst log\n\n'
      + '[task finished] "second" (t_second) exit 0 · ran 2s\n--- tail ---\nsecond log';
    const source = { kind: "text" as const, text: `2 background task events arrived:\n\n${text}` };
    expect(foldTaskEventBlocks([source])).toMatchObject([
      { kind: "task_event", taskId: "t_first", ev: "timeout", log: expect.stringContaining("first log") },
      { kind: "task_event", taskId: "t_second", ev: "finished", log: expect.stringContaining("second log") },
    ]);
    const incomplete = { ...source, text: source.text.replace(/^2 /, "3 ") };
    expect(foldTaskEventBlocks([incomplete])).toEqual([incomplete]);
  });
});
