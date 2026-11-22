/* Quick-fix D10 前端契约（源码断言风格，node 环境无 DOM，照 ConfirmModal.test.tsx）：
 * runtime 侧 post_reply=False 不再静默置 done，改为 error 态 + status_detail
 * 失败文案 + 主会话 notice 事件。前端零改动即可见——本文件钉住使它可见的
 * 三个既有渲染契约不被回退：
 *   1. MemberPanel STATUS_DOT 有 error 映射（红点、不脉冲）；
 *   2. 成员行直读 status_detail（title + 文本），失败文案因此可见；
 *   3. 事件注册表保留 notice → onNotice（失败提示的 WS 通道）。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const memberPanel = readFileSync(
  fileURLToPath(new URL("./MemberPanel.tsx", import.meta.url)),
  "utf8",
);
const handlersIndex = readFileSync(
  fileURLToPath(
    new URL("../chat/engine/handlers/index.ts", import.meta.url),
  ),
  "utf8",
);

describe("回帖失败可见性（quick-fix D10）", () => {
  it("STATUS_DOT 保留 error 态映射（红点 #ff5c5c、不脉冲）", () => {
    const m = memberPanel.match(
      /const STATUS_DOT[\s\S]*?=\s*\{[\s\S]*?\n\};/,
    );
    expect(m, "未找到 STATUS_DOT 表").toBeTruthy();
    expect(m![0]).toContain("error: { hex: \"#ff5c5c\", pulse: false }");
  });

  it("成员行直读 status_detail（文本 + title 悬浮），runtime 下发的失败文案因此可见", () => {
    expect(memberPanel).toMatch(/title=\{r\.status_detail\}/);
    expect(memberPanel).toMatch(/\{r\.status_detail \|\| tc\("team\.noDetail"\)\}/);
  });

  it("事件注册表保留 notice → onNotice（主会话广场失败提示通道）", () => {
    expect(handlersIndex).toContain('["notice", inline.onNotice]');
  });

  it("状态徽标走 team.status.<state> catalog，error 态有既定文案键", () => {
    expect(memberPanel).toContain("function statusKey(s: TeamMemberStatus)");
    expect(memberPanel).toMatch(/tr\(statusKey\(r\.status\)\)/);
  });
});
