// streamCore 的 llm.request / llm.response 配对回归（T3 LLM 请求级可观测）。
//
// 契约（runtime graph._LLM_EVENT_FIELDS + engine custom 分支白名单）：
//   request:  req_id / attempt / model / provider / messages_count
//   response: 同上 + elapsed_ms / ok / error(异常类型名) / usage(可选)
// req_id 在同一次逻辑请求的多次重试间**稳定**，attempt 逐次递增——合并键
// 必须是 (req_id, attempt)：只按 req_id 合并会让重试的成功尝试覆盖掉失败
// 尝试（失败态不可见）；不合并则 request/response 渲染成两条（刷屏）。
//
// 与 streamCoreToolEnd.test.ts 同范式：applyBlock 是纯函数，node 环境直接
// 跑真实行为（不读源码字符串、不渲染组件）。

import { describe, expect, it } from "vitest";
import { applyBlock, closePendingBlocks } from "@/components/chat/streamCore";
import type { Block } from "@/components/chat/blocks";

type LlmB = Extract<Block, { kind: "llm_call" }>;
const llm = (bs: Block[]) => bs.filter((b): b is LlmB => b.kind === "llm_call");

const REQ = {
  event: "llm.request",
  req_id: "r1",
  attempt: 1,
  model: "glm-4.6",
  provider: "openai",
  messages_count: 3,
};
const RESP = {
  event: "llm.response",
  req_id: "r1",
  attempt: 1,
  model: "glm-4.6",
  provider: "openai",
  messages_count: 3,
  ok: true,
  elapsed_ms: 1234,
  usage: {
    input_tokens: 100,
    output_tokens: 20,
    total_tokens: 120,
    cache_read_tokens: 40,
  },
};

describe("llm.request → llm.response：同 (req_id, attempt) 合并为一条并原地更新", () => {
  it("request 建一条 pending 行，带模型元数据", () => {
    const bs = applyBlock([], REQ);
    expect(llm(bs)).toHaveLength(1);
    const b = llm(bs)[0];
    expect(b.reqId).toBe("r1");
    expect(b.attempt).toBe(1);
    expect(b.pending).toBe(true);
    expect(b.model).toBe("glm-4.6");
    expect(b.provider).toBe("openai");
    expect(b.messagesCount).toBe(3);
    expect(b.elapsedMs).toBeUndefined();
  });

  it("response 原地补耗时与 token：数组长度不变、位置不变、pending 落定", () => {
    let bs = applyBlock([], { event: "token.delta", content: "前置文本" });
    bs = applyBlock(bs, REQ);
    const idx = bs.findIndex((b) => b.kind === "llm_call");
    bs = applyBlock(bs, RESP);
    expect(bs).toHaveLength(2); // 没有渲染成两条
    expect(bs.findIndex((b) => b.kind === "llm_call")).toBe(idx); // 位置不变
    const b = llm(bs)[0];
    expect(b.pending).toBe(false);
    expect(b.ok).toBe(true);
    expect(b.elapsedMs).toBe(1234);
    expect(b.inputTokens).toBe(100);
    expect(b.outputTokens).toBe(20);
    expect(b.totalTokens).toBe(120);
    expect(b.cacheReadTokens).toBe(40);
    expect(b.error).toBeUndefined();
  });

  it("request 重放幂等（重连重放不建第二条）", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, REQ);
    expect(llm(bs)).toHaveLength(1);
  });

  it("response 重放幂等（不追加、读数稳定）", () => {
    let bs = applyBlock(applyBlock([], REQ), RESP);
    bs = applyBlock(bs, RESP);
    expect(llm(bs)).toHaveLength(1);
    expect(llm(bs)[0].elapsedMs).toBe(1234);
  });

  it("response 先到（request 丢帧/孤儿流晚认领）：兜底追加已完成行，不静默丢", () => {
    const bs = applyBlock([], RESP);
    expect(llm(bs)).toHaveLength(1);
    const b = llm(bs)[0];
    expect(b.pending).toBe(false);
    expect(b.ok).toBe(true);
    expect(b.model).toBe("glm-4.6");
    expect(b.elapsedMs).toBe(1234);
  });

  it("response 元数据退化 {} 不清掉 request 已填的模型名", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, { event: "llm.response", req_id: "r1", attempt: 1, ok: true, elapsed_ms: 5 });
    expect(llm(bs)[0].model).toBe("glm-4.6");
    expect(llm(bs)[0].provider).toBe("openai");
  });

  it("usage 缺失（provider 未上报）：省略 token 字段，不造数", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, { event: "llm.response", req_id: "r1", attempt: 1, ok: true, elapsed_ms: 7 });
    const b = llm(bs)[0];
    expect(b.ok).toBe(true);
    expect(b.inputTokens).toBeUndefined();
    expect(b.outputTokens).toBeUndefined();
    expect(b.totalTokens).toBeUndefined();
  });

  it("无 req_id 的畸形帧：两个事件都 no-op（不建无法配对的行）", () => {
    expect(applyBlock([], { event: "llm.request", attempt: 1 })).toHaveLength(0);
    expect(applyBlock([], { event: "llm.response", ok: true })).toHaveLength(0);
  });

  it("attempt 缺失归 1（防御畸形帧，后端契约保证 ≥1）", () => {
    const bs = applyBlock([], { event: "llm.request", req_id: "rX" });
    expect(llm(bs)[0].attempt).toBe(1);
  });
});

describe("重试：req_id 稳定、attempt 递增——每次尝试各自一条，失败不被覆盖", () => {
  it("attempt 2 的 request 新增第二条；attempt 1 的失败态原样保留", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, {
      event: "llm.response", req_id: "r1", attempt: 1,
      ok: false, elapsed_ms: 900, error: "RuntimeError",
    });
    bs = applyBlock(bs, { ...REQ, attempt: 2 });
    expect(llm(bs)).toHaveLength(2);
    const [a1, a2] = llm(bs);
    expect(a1.attempt).toBe(1);
    expect(a1.ok).toBe(false);
    expect(a1.error).toBe("RuntimeError");
    expect(a1.pending).toBe(false);
    expect(a2.attempt).toBe(2);
    expect(a2.pending).toBe(true);
    expect(a2.reqId).toBe("r1"); // 同一逻辑请求
  });

  it("attempt 2 的 response 只更新第二条（精确配对，不污染 attempt 1）", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, { event: "llm.response", req_id: "r1", attempt: 1, ok: false, error: "RuntimeError", elapsed_ms: 900 });
    bs = applyBlock(bs, { ...REQ, attempt: 2 });
    bs = applyBlock(bs, { ...RESP, attempt: 2 });
    const [a1, a2] = llm(bs);
    expect(a1.ok).toBe(false);
    expect(a1.error).toBe("RuntimeError");
    expect(a1.inputTokens).toBeUndefined();
    expect(a2.ok).toBe(true);
    expect(a2.elapsedMs).toBe(1234);
    expect(a2.inputTokens).toBe(100);
  });

  it("不同 req_id 互不串线（一个 turn 的多次逻辑请求各自配对）", () => {
    let bs = applyBlock([], REQ);
    bs = applyBlock(bs, { ...REQ, req_id: "r2", model: "qwen-max" });
    bs = applyBlock(bs, RESP); // 只落 r1
    const rows = llm(bs);
    expect(rows).toHaveLength(2);
    expect(rows.find((b) => b.reqId === "r1")?.pending).toBe(false);
    expect(rows.find((b) => b.reqId === "r2")?.pending).toBe(true);
    expect(rows.find((b) => b.reqId === "r2")?.model).toBe("qwen-max");
  });
});

describe("llm_call 行与既有块语义的边界", () => {
  it("llm_call 行是 delta 边界：其后的 token.delta 新建文本块（时间线顺序不串）", () => {
    let bs = applyBlock([], { event: "token.delta", content: "第一次调用产出" });
    bs = applyBlock(bs, { ...REQ, req_id: "r2", attempt: 1 });
    bs = applyBlock(bs, { event: "token.delta", content: "第二次调用产出" });
    const texts = bs.filter((b) => b.kind === "text");
    expect(texts).toHaveLength(2); // 没有并进前一个 text 块
    expect(bs.map((b) => b.kind)).toEqual(["text", "llm_call", "text"]);
  });

  it("closePendingBlocks：pending 行定格为中性 interrupted（秒表不永远转）", () => {
    let bs = applyBlock([], REQ);
    bs = closePendingBlocks(bs);
    const b = llm(bs)[0];
    expect(b.pending).toBe(false);
    expect(b.ok).toBe(false);
    expect(b.error).toBe("interrupted"); // 前端哨兵：渲染灰字，不算模型失败
  });

  it("closePendingBlocks：已完成行不受影响（失败/成功终态都保留）", () => {
    let bs = applyBlock(applyBlock([], REQ), RESP);
    bs = closePendingBlocks(bs);
    expect(llm(bs)[0].ok).toBe(true);
    expect(llm(bs)[0].error).toBeUndefined();
    expect(llm(bs)[0].elapsedMs).toBe(1234);
  });

  it("closePendingBlocks：工具/问题卡既有行为不回归", () => {
    const bs = closePendingBlocks([
      { kind: "tool", id: "t1", name: "bash", content: "…", pending: true },
    ]);
    expect(bs[0]).toMatchObject({ kind: "tool", pending: false, content: "(interrupted)" });
  });
});
