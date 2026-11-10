import { describe, expect, it, beforeEach } from "vitest";

import {
  MENTION_DISPLAY_EMOJI,
  materializeMentions,
  mentionAtomicDeleteStart,
  mentionDisplayMap,
  mentionDisplayToken,
  pruneMentions,
} from "./commandMenu";

describe("mention display tokens (2026-11-10 显示形态化)", () => {
  beforeEach(() => {
    mentionDisplayMap.clear();
  });

  it("每个 mention kind 都有显示 emoji", () => {
    for (const k of ["agent", "artifact", "session", "result"]) {
      expect(MENTION_DISPLAY_EMOJI[k]).toBeTruthy();
    }
  });

  it("display token = emoji + label + 尾空格", () => {
    expect(mentionDisplayToken("artifact", "报告.md")).toBe("📎 报告.md ");
    expect(mentionDisplayToken("agent", "Dev Agent")).toBe("🤖 Dev Agent ");
  });

  it("materialize：display → 协议 token（含重复与长串优先）", () => {
    const d1 = mentionDisplayToken("artifact", "报告.md");
    const d2 = mentionDisplayToken("artifact", "报告.md.v2");
    mentionDisplayMap.set(d1, "@artifact:报告.md ");
    mentionDisplayMap.set(d2, "@artifact:报告.md.v2 ");
    // 长串（.v2）必须先替换，否则 d1 会把 d2 的前缀吃掉。
    const out = materializeMentions(`见 ${d2}和 ${d1}`, mentionDisplayMap);
    expect(out).toBe("见 @artifact:报告.md.v2 和 @artifact:报告.md ");
  });

  it("materialize：display miss（手改文本）原样降级", () => {
    const out = materializeMentions("📎 我改过的名字", mentionDisplayMap);
    expect(out).toBe("📎 我改过的名字");
  });

  it("pruneMentions：token 或 display 任一出现即保留", () => {
    const m = [{ kind: "artifact" as const, id: "x", label: "报告.md" }];
    expect(pruneMentions(m, "看 @artifact:报告.md 一下")).toHaveLength(1);
    expect(pruneMentions(m, "看 📎 报告.md 一下")).toHaveLength(0); // map 为空时不认 display
    const d = mentionDisplayToken("artifact", "报告.md");
    mentionDisplayMap.set(d, "@artifact:报告.md ");
    expect(pruneMentions(m, "看 📎 报告.md 一下")).toHaveLength(1);
    expect(pruneMentions(m, "无关文本")).toHaveLength(0);
  });

  it("atomicDelete：display / token 含空格 / token 无尾空格三形态整块删", () => {
    const d = mentionDisplayToken("agent", "Dev Agent");
    mentionDisplayMap.set(d, "@agent:Dev Agent ");
    expect(mentionAtomicDeleteStart(`你好 ${d}`, 3 + d.length)).toBe(3);
    expect(mentionAtomicDeleteStart("你好 @agent:Dev Agent ", 4 + 16)).toBe(3);
    expect(mentionAtomicDeleteStart("你好 @agent:Dev Agent", 3 + 15)).toBe(3);
    expect(mentionAtomicDeleteStart("普通文本", 4)).toBeNull();
  });
});
