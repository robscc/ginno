/**
 * sessionBlocks（sessionStream 基座纯逻辑层）单测 —— 重构批次 3-2。
 *
 * 口径：applyBlock / finalizePendingTools / mapHistoryToMsgs 逐字吸收自
 * PinStream( pm ) / DevSessionDrawer( dm ) 复制体，这里的断言就是「两
 * 消费方行为不变」的回归钉：浮窗/抽屉子集语义（tool.end 未命中不追加、
 * empty 结果过滤等）刻意与主窗口 streamCore.applyBlock 的完整版不同，
 * 不得"顺手升级"。SOCKET_TIMING 钉住 WS 生命周期四常数。
 */
import { describe, expect, it } from "vitest";
import type { Block } from "@/components/chat/blocks";
import {
  EMPTY_TOOL_RESULT_RE,
  SOCKET_TIMING,
  applyBlock,
  finalizePendingTools,
  makeMid,
  mapHistoryToMsgs,
  newTurnId,
  type SessionMsg,
} from "./sessionBlocks";

const ev = (event: string, extra: Record<string, unknown> = {}) => ({
  event,
  ...extra,
});

describe("makeMid / newTurnId", () => {
  it("makeMid 以消费方前缀顺序编号（pm/dm 调试可辨）", () => {
    const pm = makeMid("pm");
    const dm = makeMid("dm");
    expect(pm()).toBe("pm1");
    expect(pm()).toBe("pm2");
    expect(dm()).toBe("dm1");
  });

  it("newTurnId 每次唯一", () => {
    const seen = new Set(Array.from({ length: 200 }, () => newTurnId()));
    expect(seen.size).toBe(200);
  });
});

describe("applyBlock — text/thinking 流", () => {
  it("token.delta 首块创建、后续块续写", () => {
    let b: Block[] = applyBlock([], ev("token.delta", { content: "你" }));
    b = applyBlock(b, ev("token.delta", { content: "好" }));
    expect(b).toEqual([{ kind: "text", text: "你好" }]);
  });

  it("thinking.delta 独立于 text 块（不互相吞并）", () => {
    let b: Block[] = applyBlock([], ev("token.delta", { content: "答" }));
    b = applyBlock(b, ev("thinking.delta", { content: "想" }));
    b = applyBlock(b, ev("thinking.delta", { content: "想2" }));
    expect(b).toEqual([
      { kind: "text", text: "答" },
      { kind: "thinking", text: "想想2" },
    ]);
  });

  it("content 缺省兜底空串（不崩、不静默丢帧）", () => {
    expect(applyBlock([], ev("token.delta"))).toEqual([{ kind: "text", text: "" }]);
  });
});

describe("applyBlock — tool 块", () => {
  it("tool.start 建 pending 气泡并带 argsPreview", () => {
    const b = applyBlock([], ev("tool.start", { id: "t1", name: "bash", preview: "ls" }));
    expect(b).toEqual([
      {
        kind: "tool",
        id: "t1",
        name: "bash",
        content: "…",
        pending: true,
        argsPreview: "ls",
      },
    ]);
  });

  it("tool.args 按 id 精确挂预览；无 id/无 preview 为 no-op", () => {
    let b: Block[] = applyBlock([], ev("tool.start", { id: "t1", name: "bash" }));
    b = applyBlock(b, ev("tool.args", { id: "t1", preview: "pwd" }));
    expect((b[0] as { argsPreview?: string }).argsPreview).toBe("pwd");
    expect(applyBlock(b, ev("tool.args", { id: "t1" }))).toBe(b);
    expect(applyBlock(b, ev("tool.args", { preview: "x" }))).toBe(b);
  });

  it("tool.end 有 id 精确命中并收尾 pending", () => {
    let b: Block[] = applyBlock([], ev("tool.start", { id: "t1", name: "bash" }));
    b = applyBlock(b, ev("tool.end", { id: "t1", content: "ok" }));
    expect(b).toEqual([{ kind: "tool", id: "t1", name: "bash", content: "ok", pending: false, argsPreview: undefined }]);
  });

  it("tool.end 无 id 时按名字兜底到 pending 块（子集语义）", () => {
    let b: Block[] = applyBlock([], ev("tool.start", { name: "read" }));
    b = applyBlock(b, ev("tool.end", { name: "read", content: "data" }));
    expect((b[0] as { content: string; pending: boolean }).content).toBe("data");
    expect((b[0] as { pending: boolean }).pending).toBe(false);
  });

  it("tool.end 未命中任何块时内容不变（子集语义：不追加兜底块）", () => {
    const b: Block[] = [{ kind: "text", text: "hi" }];
    expect(applyBlock(b, ev("tool.end", { id: "ghost", content: "x" }))).toEqual(b);
  });

  it("empty 结果工具块被过滤（噪音白名单）", () => {
    let b: Block[] = applyBlock([], ev("tool.start", { id: "t1", name: "glob" }));
    b = applyBlock(b, ev("tool.start", { id: "t2", name: "keep" }));
    b = applyBlock(b, ev("tool.end", { id: "t1", content: "(no matches)" }));
    expect(b).toHaveLength(1);
    expect((b[0] as { id?: string }).id).toBe("t2");
  });

  it("EMPTY_TOOL_RESULT_RE 覆盖全部白名单模式", () => {
    for (const s of [
      "(no matches)",
      "(no files found)",
      "(empty)",
      "no results",
      "no files matched",
      "no matches found",
      "no files found",
      "(nothing found)",
      "  (Empty)  ",
    ]) {
      expect(EMPTY_TOOL_RESULT_RE.test(s)).toBe(true);
    }
    expect(EMPTY_TOOL_RESULT_RE.test("3 files")).toBe(false);
  });
});

describe("applyBlock — image.emit（Pin 超集；Dev 抽屉接入后获得）", () => {
  it("产图事件落 image 块", () => {
    const b = applyBlock([], ev("image.emit", { file_id: "f1", name: "a.png", mtime: 1 }));
    expect(b).toEqual([{ kind: "image", fileId: "f1", name: "a.png", mtime: 1 }]);
  });
});

describe("applyBlock — 未知事件", () => {
  it("usage / run.* 等子集外事件原样返回", () => {
    const b: Block[] = [{ kind: "text", text: "x" }];
    for (const e of ["usage", "run.status", "goal.progress", "todos.changed"]) {
      expect(applyBlock(b, ev(e))).toBe(b);
    }
  });
});

describe("finalizePendingTools", () => {
  it("pending 的「…」占位换中断文案，已完成块不动", () => {
    const blocks: Block[] = [
      { kind: "tool", name: "a", content: "…", pending: true },
      { kind: "tool", name: "b", content: "done", pending: false },
      { kind: "text", text: "t" },
    ];
    expect(finalizePendingTools(blocks, "（已中断）")).toEqual([
      { kind: "tool", name: "a", content: "（已中断）", pending: false },
      { kind: "tool", name: "b", content: "done", pending: false },
      { kind: "text", text: "t" },
    ]);
  });

  it("pending 但已有部分输出的块保留输出（只翻 pending 标记）", () => {
    const blocks: Block[] = [{ kind: "tool", name: "a", content: "partial", pending: true }];
    expect(finalizePendingTools(blocks, "X")).toEqual([
      { kind: "tool", name: "a", content: "partial", pending: false },
    ]);
  });
});

interface TestMsg extends SessionMsg {
  retryText?: string;
  tag?: string;
}

describe("mapHistoryToMsgs", () => {
  it("基础映射：id 缺省走 mid、role 归一、blocks/turnId 透传", () => {
    const mid = makeMid("dm");
    const msgs = mapHistoryToMsgs<TestMsg>(
      {
        messages: [
          { role: "user", blocks: [{ kind: "text", text: "hi" }], turnId: "t-1" },
          { id: "srv-2", role: "assistant", blocks: [{ kind: "text", text: "yo" }] },
        ],
      },
      mid,
    );
    expect(msgs).toEqual([
      { id: "dm1", role: "user", blocks: [{ kind: "text", text: "hi" }], turnId: "t-1" },
      { id: "srv-2", role: "assistant", blocks: [{ kind: "text", text: "yo" }], turnId: undefined },
    ]);
  });

  it("last_error → 红卡；默认不带 retryText（Dev 抽屉语义）", () => {
    const mid = makeMid("dm");
    const msgs = mapHistoryToMsgs<TestMsg>(
      {
        messages: [{ id: "u1", role: "user", blocks: [{ kind: "text", text: "问" }] }],
        last_error: { message: "boom" },
      },
      mid,
    );
    expect(msgs).toHaveLength(2);
    expect(msgs[1]).toEqual({
      id: "dm1",
      role: "assistant",
      error: true,
      blocks: [{ kind: "text", text: "boom" }],
    });
    expect(msgs[1].retryText).toBeUndefined();
  });

  it("withRetryText：取最后一条 user 消息文本（Pin 重试按钮契约）", () => {
    const mid = makeMid("pm");
    const msgs = mapHistoryToMsgs<TestMsg>(
      {
        messages: [{ id: "u1", role: "user", blocks: [{ kind: "text", text: "重发我" }] }],
        last_error: { message: "kaboom" },
      },
      mid,
      { withRetryText: true },
    );
    expect(msgs[1].retryText).toBe("重发我");
  });

  it("errorText：i18n key 翻译覆盖英文 fallback（Pin evText 契约）", () => {
    const mid = makeMid("pm");
    const msgs = mapHistoryToMsgs<TestMsg>(
      { last_error: { message: "raw", i18n_key: "err.x", params: { n: 3 } } },
      mid,
      { errorText: (le, fallback) => `${le.i18n_key}:${le.params?.n}!${fallback}` },
    );
    expect(msgs[0].blocks[0]).toEqual({ kind: "text", text: "err.x:3!raw" });
  });

  it("decorate：历史条目扩展字段透传（Pin teamReply 契约）", () => {
    const mid = makeMid("pm");
    const msgs = mapHistoryToMsgs<TestMsg>(
      { messages: [{ id: "a1", role: "assistant", blocks: [] }] },
      mid,
      { decorate: (raw, msg) => ({ ...msg, tag: `team:${raw.id}` }) },
    );
    expect(msgs[0].tag).toBe("team:a1");
  });

  it("无 messages/无 last_error → 空数组", () => {
    expect(mapHistoryToMsgs<TestMsg>({}, makeMid("dm"))).toEqual([]);
  });
});

describe("SOCKET_TIMING —— WS 生命周期四常数（三复制体行为基线）", () => {
  it("3s 重连 / 20s ping / 45s 静默强断（10s 巡检）/ 探针 6s 兜底", () => {
    expect(SOCKET_TIMING).toEqual({
      RECONNECT_MS: 3000,
      PING_MS: 20000,
      SILENCE_CLOSE_MS: 45000,
      WATCH_TICK_MS: 10000,
      RECONCILE_FALLBACK_MS: 6000,
    });
  });
});
