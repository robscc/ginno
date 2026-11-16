/* NoticeHost 次按钮渲染的源码断言（node 环境无 DOM，照仓库既有范式）。
 *
 * lastwords 恢复提示（2026-11-16）需要「恢复 / 忽略」两个明确决策按钮。
 * 通知总线加了 secondaryAction 字段，本文件钉住渲染层真的把它画出来、
 * 且语义正确：次按钮 = 已读 + 执行决策回调；× 关闭仍只是「已读」，
 * 绝不承载「拒绝恢复」的语义（那会误伤只想收卡稍后处理的用户）。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./NoticeHost.tsx", import.meta.url)),
  "utf8",
);

describe("NoticeCard 的 secondaryAction 渲染", () => {
  it("渲染条件覆盖 action 或 secondaryAction（只有次按钮也要画按钮行）", () => {
    expect(SRC).toContain("(n.action || n.secondaryAction)");
  });

  it("次按钮点击 = markNoticeRead + 执行 secondaryAction.onClick", () => {
    const idx = SRC.indexOf("{n.secondaryAction && (");
    expect(idx, "未找到 secondaryAction 渲染块").toBeGreaterThan(-1);
    const block = SRC.slice(idx, idx + 900);
    expect(block).toContain("markNoticeRead(n.id, n.dedupeKey)");
    expect(block).toContain("n.secondaryAction!.onClick()");
    expect(block).toContain("{n.secondaryAction.label}");
  });

  it("主按钮行为不回退：仍是 markNoticeRead + action.onClick", () => {
    const idx = SRC.indexOf("{n.action && (");
    expect(idx).toBeGreaterThan(-1);
    const block = SRC.slice(idx, idx + 900);
    expect(block).toContain("markNoticeRead(n.id, n.dedupeKey)");
    expect(block).toContain("n.action!.onClick()");
  });

  it("× 关闭仍只是已读（不夹带决策回调）", () => {
    const idx = SRC.indexOf("aria-label={t(\"notice.dismiss\")}");
    expect(idx).toBeGreaterThan(-1);
    // 精确截取 × 按钮自身（最后一个 <button 到 aria-label 之间）
    const btnStart = SRC.lastIndexOf("<button", idx);
    const before = SRC.slice(btnStart, idx);
    expect(before).toContain("onClick={() => markNoticeRead(n.id, n.dedupeKey)}");
    expect(before).not.toContain("secondaryAction");
  });
});
