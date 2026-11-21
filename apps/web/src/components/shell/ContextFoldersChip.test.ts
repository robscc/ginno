// ContextFoldersChip.pickDropdownAlign（2026-11-21 自适应 flip）纯函数契约。
// 默认右锚（chip 恒在 composer 最右）；右侧不足且左侧更宽 → 翻左。

import { describe, expect, it } from "vitest";
import { pickDropdownAlign } from "@/components/shell/ContextFoldersChip";

describe("pickDropdownAlign", () => {
  it("右侧空间充足：右锚", () => {
    // 视口 1440，chip 右缘 900 → 右侧 540 > 384+16
    expect(pickDropdownAlign(1440, { left: 700, right: 900 })).toBe("right");
  });
  it("右侧不足且左侧更宽：翻左", () => {
    // 视口 1440，chip 右缘 1380 → 右侧 60；左侧 1380 > 400
    expect(pickDropdownAlign(1440, { left: 1200, right: 1380 })).toBe("left");
  });
  it("两侧都不足：保持右锚（max-w 的 100vw 兜底收缩）", () => {
    // 视口 420，chip 居中 → 左右都不足 400
    expect(pickDropdownAlign(420, { left: 160, right: 260 })).toBe("right");
  });
  it("rect 缺失：默认右锚", () => {
    expect(pickDropdownAlign(1440, null)).toBe("right");
  });
});
