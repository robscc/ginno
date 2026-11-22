import { describe, expect, it } from "vitest";

import { MAX_SEND_TEXT_BYTES, textByteLength } from "@/lib/limits";

describe("limits（D11：WS 文本帧上限）", () => {
  it("上限为 1MB，与后端 ws/loop.py MAX_INBOUND_TEXT_BYTES 同值", () => {
    expect(MAX_SEND_TEXT_BYTES).toBe(1_048_576);
  });

  it("textByteLength 按 UTF-8 计字节（CJK 每字 3 字节），非字符数", () => {
    expect(textByteLength("abc")).toBe(3);
    expect(textByteLength("长")).toBe(3);
    expect(textByteLength("长长长")).toBe(9); // 3 字 > 文本长度 3 —— 字节口径才与 WS 帧一致
    expect(textByteLength("")).toBe(0);
  });

  it("边界：恰 1MB 通过、1MB+1 拒（与后端 > 判定一致）", () => {
    const exactly = "a".repeat(MAX_SEND_TEXT_BYTES);
    expect(textByteLength(exactly)).toBe(MAX_SEND_TEXT_BYTES);
    expect(textByteLength(exactly + "a")).toBe(MAX_SEND_TEXT_BYTES + 1);
  });
});
