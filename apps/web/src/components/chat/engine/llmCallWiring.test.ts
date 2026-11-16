// llm.request / llm.response 渲染链路接线断言（T3 LLM 请求级可观测）。
// node 环境无 DOM，照 AgentChips.test.tsx 范式读源码字符串钉住分发/渲染层
// 契约；reducer 的真实行为由 streamCoreLlmCall.test.ts 覆盖，渲染真实性由
// packaged UI e2e（test_packaged_ui_llm_events.py）覆盖。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enChat from "../../../../messages/en/chat.json";
import zhChat from "../../../../messages/zh-CN/chat.json";

const HANDLE = readFileSync(
  fileURLToPath(new URL("./handle.ts", import.meta.url)),
  "utf8",
);
const BLOCKS = readFileSync(
  fileURLToPath(new URL("../blocks.tsx", import.meta.url)),
  "utf8",
);
const LINE = readFileSync(
  fileURLToPath(new URL("../blocks/llmCallLine.tsx", import.meta.url)),
  "utf8",
);

describe("handle.ts：llm.* 事件分发（独立 case 体，不进 5 事件共享体）", () => {
  it("llm.request / llm.response 走 adoptOrphanStream + mutateLive（tool 帧同范式）", () => {
    expect(HANDLE).toMatch(/case "llm\.request":\s*\n\s*case "llm\.response":\s*\n\s*adoptOrphanStream\(ctx, sid\);\s*\n\s*mutateLive\(ctx, sid, ev\);\s*\n\s*break;/);
  });

  it("turn.stopped 收口闸门覆盖 pending llm_call（停在模型调用中途也要定格）", () => {
    expect(HANDLE).toMatch(/hasPendingTool\(msg\.blocks\) \|\| hasPendingLlmCall\(msg\.blocks\)/);
  });

  it("error 收口闸门同样覆盖（失败气泡里的「请求中…」不永远转）", () => {
    expect(HANDLE).toMatch(/if \(!hasPendingTool\(marked\.blocks\) && !hasPendingLlmCall\(marked\.blocks\)\) return marked;/);
  });
});

describe("blocks.tsx：llm_call 渲染分支与收口谓词", () => {
  it("InnerBlocks 按 (reqId, attempt) 稳定 key 渲染 LlmCallLine（原地更新复用 DOM）", () => {
    expect(BLOCKS).toContain('import { LlmCallLine } from "./blocks/llmCallLine"');
    expect(BLOCKS).toMatch(/b\.kind === "llm_call"/);
    expect(BLOCKS).toContain("key={`llm-${b.reqId}-${b.attempt}`}");
  });

  it("hasPendingLlmCall 独立导出，不并入 hasPendingTool（running 语义不变）", () => {
    expect(BLOCKS).toMatch(/export function hasPendingLlmCall\(blocks: Block\[\]\): boolean/);
    // 只取 hasPendingTool 的函数体（到首个行首 } 为止），不含后续注释。
    const start = BLOCKS.indexOf("export function hasPendingTool");
    const bodyEnd = BLOCKS.indexOf("\n}", start);
    const hpt = BLOCKS.slice(start, bodyEnd);
    expect(start).toBeGreaterThan(-1);
    expect(hpt).not.toContain("llm_call");
  });
});

describe("LlmCallLine：紧凑行内条目的可见性契约", () => {
  it("e2e 定位点 data-testid=llm-call-line", () => {
    expect(LINE).toContain('data-testid="llm-call-line"');
  });

  it("attempt>1 显示重试角标（↻{n} + llm-call-attempt tooltip）", () => {
    expect(LINE).toMatch(/\{block\.attempt > 1 &&/);
    expect(LINE).toContain("↻{block.attempt}");
    expect(LINE).toContain('tc("llm-call-attempt", { n: block.attempt })');
  });

  it("pending 显示「请求中…」脉冲；失败显眼但不刺眼（琥珀，非正红）", () => {
    expect(LINE).toContain('tc("llm-call-pending")');
    expect(LINE).toMatch(/block\.pending \?/);
    expect(LINE).toContain('tc("llm-call-failed")');
    expect(LINE).toContain("text-amber-600 dark:text-amber-400");
    expect(LINE).not.toContain("text-red-600"); // 正红留给 turn 级失败卡
  });

  it("interrupted 哨兵走中性灰（turn 中止 ≠ 模型失败）", () => {
    expect(LINE).toContain('block.error === "interrupted"');
    expect(LINE).toContain('tc("llm-call-interrupted")');
  });

  it("耗时 + token 复用 turn_meta 胶囊语言（↑输入蓝 / ↓输出绿 / fmtTokens）", () => {
    expect(LINE).toContain("rounded-md bg-card2/60 px-1.5 py-px");
    expect(LINE).toContain("text-sky-600 dark:text-sky-400");
    expect(LINE).toContain("text-emerald-600 dark:text-emerald-400");
    expect(LINE).toContain("fmtTokens(block.inputTokens)");
    expect(LINE).toContain("fmtTokens(block.outputTokens)");
  });

  it("模型名缺失回退 locale 无关的 LLM（后端元数据提取退化 {} 时不空白）", () => {
    expect(LINE).toContain('block.model || "LLM"');
  });
});

describe("i18n：llm-call-* 新键双语齐全且扁平连字符（无点）", () => {
  const keys = [
    "llm-call-pending",
    "llm-call-attempt",
    "llm-call-failed",
    "llm-call-interrupted",
    "llm-call-messages",
  ] as const;
  for (const k of keys) {
    it(`${k} 在 en 与 zh-CN 都存在、非空、已翻译`, () => {
      expect(k).not.toContain("."); // 扁平键（带点 key 曾炸 IntlError）
      const en = (enChat.chat as Record<string, unknown>)[k];
      const zh = (zhChat.chat as Record<string, unknown>)[k];
      expect(typeof en).toBe("string");
      expect(typeof zh).toBe("string");
      expect((en as string).length).toBeGreaterThan(0);
      expect((zh as string).length).toBeGreaterThan(0);
      expect(en).not.toBe(zh);
    });
  }

  it("llm-call-attempt / llm-call-messages 带 {n} 占位（双语一致）", () => {
    for (const k of ["llm-call-attempt", "llm-call-messages"] as const) {
      expect((enChat.chat as Record<string, unknown>)[k] as string).toContain("{n}");
      expect((zhChat.chat as Record<string, unknown>)[k] as string).toContain("{n}");
    }
  });
});
