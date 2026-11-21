// subagentParse.parseSubagentResults 正则化（2026-11-22）契约。
// 背景：text/html DOMParser 对「前缀文本 + 多信封」合并注入（正文富
// markdown：表格竖线、<think> 尖括号行）会漏抓信封 → live echo 把整段
// XML 插成 user 气泡 → 空泡 + 裸 turnId chip（turn 75320061 实机）。
// 修复后与服务端 messages_ui._RESULT_ENV_RE 完全同构，node 环境可测。

import { describe, expect, it } from "vitest";
import {
  parseSubagentResults,
  parseSubagentResult,
  foldSubagentResultBlocks,
} from "@/components/chat/blocks/subagentParse";

const MERGED = `以下是 2 个子代理的回传（结果与失败报告）：

<ginno_subagent_result session="5d5f846715f04a5fac7abbe8d9c86482" goal="在 /tmp/ginno-audit-main（分支 fix/compaction-audit）审计压缩链">侧 (0,1.0) |
| C sid 路由 | ✅ | 超出 goal: 另修 |
| D 20000 稀融 | ✅ | 含 [compact] 标记 |

## 遗留问题
- <think> 这类尖括号行不破坏解析
</ginno_subagent_result>

<ginno_subagent_result session="6d9538fcc2824783bbf8c864ba9aa187" goal="另一个目标 &amp; 特殊实体">正文B…</ginno_subagent_result>`;

describe("parseSubagentResults（正则化后）", () => {
  it("合并注入：前缀文本 + 多信封全部抓到", () => {
    const r = parseSubagentResults(MERGED);
    expect(r.length).toBe(2);
    expect(r[0].sessionId).toBe("5d5f846715f04a5fac7abbe8d9c86482");
    expect(r[1].sessionId).toBe("6d9538fcc2824783bbf8c864ba9aa187");
  });
  it("富 markdown 正文完整保留（表格/尖括号行）", () => {
    const r = parseSubagentResults(MERGED);
    expect(r[0].summary).toContain("| C sid 路由 |");
    expect(r[0].summary).toContain("<think>");
    expect(r[0].summary).toContain("## 遗留问题");
  });
  it("属性实体解码（goal 含 &amp;）", () => {
    const r = parseSubagentResults(MERGED);
    expect(r[1].goal).toContain("& 特殊实体");
    expect(r[1].goal).not.toContain("&amp;");
  });
  it("单信封兼容（parseSubagentResult 取第一条）", () => {
    const one = `<ginno_subagent_result session="abc" goal="g">摘要正文</ginno_subagent_result>`;
    const r = parseSubagentResult(one);
    expect(r?.sessionId).toBe("abc");
    expect(r?.summary).toBe("摘要正文");
  });
  it("无信封：返回空（回落 steer 带/原文渲染）", () => {
    expect(parseSubagentResults("普通用户消息，没有协议标签")).toEqual([]);
  });
  it("fold：合并注入折出 2 张卡，前缀文本不残留 XML", () => {
    const out = foldSubagentResultBlocks([{ kind: "text" as const, text: MERGED }]);
    const cards = out.filter((b) => b.kind === "subagent_result");
    expect(cards.length).toBe(2);
    const texts = out.filter((b) => b.kind === "text") as Array<{ text: string }>;
    for (const t of texts) {
      expect(t.text).not.toContain("<ginno_subagent_result");
    }
  });
});
