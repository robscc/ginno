/* 移除运行中成员的防呆文案契约（R3 巡检 Q5，2026-11-22）。
 *
 * node 环境源码断言风格（照 ConfirmModal.test.tsx / ConnectorDetail.test.tsx
 * 的仓库纪律——无 DOM 不渲染组件；真实浏览器行为由 packaged UI e2e
 * test_packaged_ui_team_remove_guard.py 覆盖，两层分工见 vitest.config）。
 *
 * 背景：移除成员是级联删全部会话的不可逆动作，但确认框对「正在运行的
 * 成员」没有说明后果——用户看不到「会强停 turn」这一层。对照「解散团队」
 * 已有的活 turn 防呆范式（team.disbandBlocked 点名运行成员），移除单个
 * 成员同样要在确认框里说清楚。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLTopathFix("./MemberPanel.tsx"),
  "utf8",
);
const ZH = JSON.parse(
  readFileSync(fileURLTopathFix("../../../messages/zh-CN/chat.json"), "utf8"),
);
const EN = JSON.parse(
  readFileSync(fileURLTopathFix("../../../messages/en/chat.json"), "utf8"),
);

function fileURLTopathFix(rel: string): string {
  return fileURLToPath(new URL(rel, import.meta.url));
}

describe("移除成员确认框：运行中成员必须用防呆文案", () => {
  it("message 按 status 分流：working/blocked → removeMemberRunningMsg", () => {
    // 三元分流存在，且两种活 turn 形态（working=在跑，blocked=parked 等输入）
    // 都命中防呆文案 —— 二者移除时都会被强停。
    expect(SRC).toMatch(
      /confirmRemove\.status === "working" \|\| confirmRemove\.status === "blocked"/,
    );
    expect(SRC).toContain('tc("team.removeMemberRunningMsg"');
  });

  it("空闲成员保持既有文案（removeMemberConfirmMsg）不被误伤", () => {
    expect(SRC).toContain('tc("team.removeMemberConfirmMsg"');
    // 分流是同一 message 属性上的三元：两分支共存
    const m = SRC.match(
      /message=\{[\s\S]*?removeMemberRunningMsg[\s\S]*?removeMemberConfirmMsg[\s\S]*?\}\s*$/,
    );
    expect(m, "防呆文案与原文案必须在同一个 message 三元里").toBeTruthy();
  });

  it("i18n 双语 key 齐备且都带 {name} 占位符", () => {
    for (const catalog of [ZH, EN]) {
      const team = (catalog.chat as Record<string, unknown>).team as Record<string, string>;
      expect(team.removeMemberRunningMsg, "chat.team.removeMemberRunningMsg 缺失").toBeTruthy();
      expect(team.removeMemberRunningMsg).toContain("{name}");
      expect(team.removeMemberConfirmMsg).toContain("{name}");
    }
  });

  it("防呆文案点名后果：强停 turn + 全部会话不可恢复（zh/en 都要）", () => {
    expect(ZH.chat.team.removeMemberRunningMsg).toContain("强停");
    expect(ZH.chat.team.removeMemberRunningMsg).toContain("不可恢复");
    expect(EN.chat.team.removeMemberRunningMsg).toMatch(/force-stop its turn/);
    expect(EN.chat.team.removeMemberRunningMsg).toMatch(/[Cc]annot be undone/);
  });
});
