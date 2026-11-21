/* TasksOverviewPanel + RightPanel/入口接线的源码断言测试——node 无 DOM，照
 * RemotePeerBadge.test.tsx 范式；渲染真实性由 packaged UI e2e 覆盖
 * （vitest.config 注释的既定分工）。纯逻辑（分组/停止/回滚）在
 * lib/tasksOverview.test.ts。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enRight from "../../../messages/en/right.json";
import zhRight from "../../../messages/zh-CN/right.json";

const SRC = readFileSync(
  fileURLToPath(new URL("./TasksOverviewPanel.tsx", import.meta.url)),
  "utf8",
);
const RIGHT_PANEL = readFileSync(
  fileURLToPath(new URL("./RightPanel.tsx", import.meta.url)),
  "utf8",
);
const SIDEBAR_HEADER = readFileSync(
  fileURLToPath(new URL("../shell/SidebarHeader.tsx", import.meta.url)),
  "utf8",
);
const APP_SHELL = readFileSync(
  fileURLToPath(new URL("../shell/AppShell.tsx", import.meta.url)),
  "utf8",
);

const KINDS = ["turn", "schedule", "plan", "run", "goal", "delegation"] as const;

describe("TasksOverviewPanel 渲染结构", () => {
  it("六组分组：data-group=kind + 组计数徽标 + 组头图标", () => {
    expect(SRC).toContain("data-group={grp.kind}");
    expect(SRC).toContain("tasks-overview-count-${grp.kind}");
    for (const ic of ["MessageSquare", "AlarmClock", "Bot", "WorkflowIcon", "Flag", "Link2"]) {
      expect(SRC).toContain(ic);
    }
  });

  it("空态：groups.length === 0 → 空态块（🎉 文案键 empty）", () => {
    expect(SRC).toMatch(/groups\.length === 0 &&/);
    expect(SRC).toContain('data-testid="tasks-overview-empty"');
    expect(SRC).toContain('t("empty")');
  });

  it("禁用态：stoppable=false → 按钮置灰 + title 显示 stop_hint", () => {
    expect(SRC).toContain("const stopDisabled = h.stoppable === false;");
    expect(SRC).toContain("title={stopDisabled ? stopHint");
    expect(SRC).toContain("h.stop_hint || t(");
    expect(SRC).toMatch(/disabled=\{stopDisabled\}/);
    expect(SRC).toMatch(/stopDisabled\s*\?\s*"cursor-not-allowed/);
  });

  it("stoppable=false 且可跳转 → 「查看」按钮（原型语义：禁停行以查看为主 affordance）", () => {
    expect(SRC).toMatch(/\{stopDisabled && jump && \(/);
    expect(SRC).toContain('t("view")');
  });

  it("停止 → ConfirmModal 薄确认，文案按 schedule/其余分叉", () => {
    expect(SRC).toContain("<ConfirmModal");
    expect(SRC).toContain("stopConfirmVariant(confirmTarget.kind) === \"pause\"");
    expect(SRC).toContain('t("confirmBodyPause", { title: confirmTarget.title })');
    expect(SRC).toContain('t("confirmBodyIrreversible", { title: confirmTarget.title })');
    expect(SRC).toContain('confirmLabel={t(`stopBtn.${confirmTarget.kind}`)}');
  });

  it("停止动线：确认后走 stopHandleWithRollback（乐观移除+失败回滚），错误落 stopError 条", () => {
    expect(SRC).toContain("stopHandleWithRollback(h)");
    expect(SRC).toContain('t("stopFailed", { title: h.title })');
    expect(SRC).toContain('data-testid="tasks-overview-stop-error"');
  });

  it("行/查看点击 → openHandle：run→workflow 面板、schedule→/scheduled、其余→open_session 三步", () => {
    expect(SRC).toContain('g.setRightTab("workflow")');
    expect(SRC).toContain('router.push("/scheduled")');
    expect(SRC).toContain("g.setActiveSession(target.sid)");
    expect(SRC).toContain('new CustomEvent("ginno:focus-latest", { detail: target.sid })');
  });

  it("数据源：hub 订阅（进 tab 即拉）+ 手动刷新；不做 WS", () => {
    expect(SRC).toContain("subscribeTasksOverview(setSnap)");
    expect(SRC).toContain("refreshTasksOverview()");
    expect(SRC).not.toContain("WebSocket");
  });

  it("e2e 定位点：面板/行/停止按钮 testid", () => {
    expect(SRC).toContain('data-testid="tasks-overview-panel"');
    expect(SRC).toContain('data-testid={`tasks-overview-item-${h.kind}`}');
    expect(SRC).toContain('data-testid={`tasks-overview-stop-${h.kind}`}');
  });
});

describe("RightPanel / 入口接线", () => {
  it("RightPanel：running tab 挂载面板 + 总徽标（hub 订阅 + bg-blue 脉冲）", () => {
    expect(RIGHT_PANEL).toContain('import { TasksOverviewPanel } from "./TasksOverviewPanel"');
    expect(RIGHT_PANEL).toContain('{tab === "running" && <TasksOverviewPanel />}');
    expect(RIGHT_PANEL).toContain("subscribeTasksOverview");
    expect(RIGHT_PANEL).toMatch(/meta\.id === "running" && runningTotal > 0/);
    expect(RIGHT_PANEL).toContain('tb("running", { n: runningTotal })');
  });

  it("侧栏品牌行 ▶N 徽标：点击 = 回工作区 + running tab + 展开右栏", () => {
    expect(SIDEBAR_HEADER).toContain('data-testid="sidebar-running-badge"');
    expect(SIDEBAR_HEADER).toContain('g.setRightTab("running")');
    expect(SIDEBAR_HEADER).toContain('g.setRightPanelOpen(true)');
  });

  it("AppShell 注册 __ginnoOpenTasksOverview 桥（menubar ▶N 下拉「查看全部」的落点）", () => {
    expect(APP_SHELL).toContain("openTasksOverview = () => {");
    expect(APP_SHELL).toContain(".__ginnoOpenTasksOverview =");
    expect(APP_SHELL).toContain('setRightTab("running")');
  });
});

describe("i18n：right.json 双语键齐全", () => {
  const zh = (zhRight.right as Record<string, unknown>)["tasksOverview"] as Record<string, unknown>;
  const en = (enRight.right as Record<string, unknown>)["tasksOverview"] as Record<string, unknown>;

  it("tabs.running / badges.running 双语", () => {
    expect((zhRight.right.tabs as Record<string, string>)["running"]).toBe("运行");
    expect((enRight.right.tabs as Record<string, string>)["running"]).toBe("Running");
    expect((zhRight.right.badges as Record<string, string>)["running"]).toContain("{n}");
    expect((enRight.right.badges as Record<string, string>)["running"]).toContain("{n}");
  });

  it("六组的 groups/stopBtn/confirmTitle 键 + 确认文案两分支 + 空态/错误/查看", () => {
    for (const k of KINDS) {
      expect((zh.groups as Record<string, string>)[k]).toBeTruthy();
      expect((en.groups as Record<string, string>)[k]).toBeTruthy();
      expect((zh.stopBtn as Record<string, string>)[k]).toBeTruthy();
      expect((en.stopBtn as Record<string, string>)[k]).toBeTruthy();
      expect((zh.confirmTitle as Record<string, string>)[k]).toBeTruthy();
      expect((en.confirmTitle as Record<string, string>)[k]).toBeTruthy();
    }
    for (const key of [
      "title",
      "refresh",
      "empty",
      "loadFailed",
      "nextFire",
      "external",
      "notStoppable",
      "view",
      "stopFailed",
      "confirmBodyPause",
      "confirmBodyIrreversible",
      "badgeEntry",
    ]) {
      expect(typeof zh[key]).toBe("string");
      expect(typeof en[key]).toBe("string");
    }
  });

  it("确认文案语义：schedule=暂停可恢复（zh 提到「恢复」），其余=不可撤销（en 提到 cannot be undone）", () => {
    expect(zh.confirmBodyPause as string).toContain("恢复");
    expect(en.confirmBodyPause as string).toContain("resume");
    expect(zh.confirmBodyIrreversible as string).toContain("不可撤销");
    expect(en.confirmBodyIrreversible as string).toContain("cannot be undone");
  });

  it("stopFailed 双语都带 {title} 占位（面板传参）", () => {
    expect(zh.stopFailed as string).toContain("{title}");
    expect(en.stopFailed as string).toContain("{title}");
  });
});
