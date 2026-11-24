/* TasksOverviewPanel + RightPanel/入口接线的源码断言测试——node 无 DOM，照
 * RemotePeerBadge.test.tsx 范式；渲染真实性由 packaged UI e2e 覆盖
 * （vitest.config 注释的既定分工）。纯逻辑（三档分组/停止/回滚）在
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

const SHELL_ACTIVITY = readFileSync(
  fileURLToPath(new URL("../shell/useShellActivity.ts", import.meta.url)),
  "utf8",
);

const KINDS = ["turn", "schedule", "plan", "run", "goal", "delegation"] as const;
const PHASES = ["running", "waiting", "scheduled"] as const;

describe("TasksOverviewPanel 渲染结构", () => {
  it("三档分组（执行中/等你处理/已排期）：data-phase 段 + 段计数，段内保留六类组头", () => {
    // 段头：phase 驱动 + 段计数 testid；顺序由 lib 层 TASK_PHASE_ORDER 决定
    expect(SRC).toContain("data-phase={ph.phase}");
    expect(SRC).toContain('data-testid={`tasks-overview-phase-${ph.phase}`}');
    expect(SRC).toContain('data-testid={`tasks-overview-phase-count-${ph.phase}`}');
    expect(SRC).toContain("overviewPhaseGroups(data)");
    // 段内二级组头：data-group=kind + 组计数徽标 + 组头图标（六类可识别性保留）
    expect(SRC).toContain("data-group={grp.kind}");
    expect(SRC).toContain("tasks-overview-count-${grp.kind}");
    for (const ic of ["MessageSquare", "AlarmClock", "Bot", "WorkflowIcon", "Flag", "Link2"]) {
      expect(SRC).toContain(ic);
    }
    // 三档段头图标
    for (const ic of ["Activity", "Hourglass", "CalendarClock"]) {
      expect(SRC).toContain(ic);
    }
  });

  it("空态：showEmpty（loaded && ok && 零段）→ 空态块（🎉 文案键 empty，失败不伪装成真空）", () => {
    expect(SRC).toMatch(/const showEmpty = loaded && fetchOk && phases\.length === 0/);
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

  it("标题 = 真正的导航控件：原生 button（可 Tab/Enter），DOM 序在停止按钮之前；不可跳转退化为纯文本", () => {
    expect(SRC).toContain('type="button"');
    expect(SRC).toContain('aria-label={t("openTitle", { title: h.title })}');
    expect(SRC).toMatch(/jump \? \(/); // 有跳转目标 → 标题按钮；无 → 纯文本分支
    // Tab 序 = 视觉序：标题按钮的 openHandle 出现位置先于停止按钮 testid
    const titleBtnPos = SRC.indexOf('aria-label={t("openTitle", { title: h.title })}');
    const stopBtnPos = SRC.indexOf("tasks-overview-stop-${h.kind}");
    expect(titleBtnPos).toBeGreaterThan(-1);
    expect(stopBtnPos).toBeGreaterThan(-1);
    expect(titleBtnPos).toBeLessThan(stopBtnPos);
  });

  it("无嵌套交互：行容器不带 onClick（跳转/停止各归各的控件），无需 stopPropagation", () => {
    // 行容器开标签：key → className → testid → handle-id →（可选 phase）→ 关闭，期间不得出现 onClick
    const row = SRC.match(/<div\s+key=\{handleKey\(h\)\}[\s\S]*?data-handle-id=\{h\.id\}[^>]*>/);
    expect(row).toBeTruthy();
    expect(row![0]).not.toContain("onClick");
    // 旧实现靠行级 onClick + stopPropagation；重构后整文件不应再有行级冒泡处理
    expect(SRC).not.toContain("stopPropagation");
  });

  it("「查看」按钮退役：跳转职能收敛到标题导航控件（t(\"view\") 不得残留）", () => {
    expect(SRC).not.toContain('t("view")');
    expect(SRC).not.toContain("stopDisabled && jump");
  });

  it("停止 → ConfirmModal 薄确认，文案按 schedule/其余分叉", () => {
    expect(SRC).toContain("<ConfirmModal");
    expect(SRC).toContain('stopConfirmVariant(confirmTarget.kind) === "pause"');
    expect(SRC).toContain('t("confirmBodyPause", { title: confirmTarget.title })');
    expect(SRC).toContain('t("confirmBodyIrreversible", { title: confirmTarget.title })');
    expect(SRC).toContain('confirmLabel={t(`stopBtn.${confirmTarget.kind}`)}');
  });

  it("行/停止身份用完整 key（handleKey）：plan_id 全局不唯一，须连 session_id 区分", () => {
    expect(SRC).toContain("key={handleKey(h)}");
    expect(SRC).toContain("stoppingIds.has(handleKey(h))");
    expect(SRC).not.toMatch(/`\$\{h\.kind\}:\$\{h\.id\}`/); // 旧的不完整身份键不得残留
  });

  it("三态：未加载（loading）/ 失败可重试（load-error + retry）/ 真空态（ok 才 🎉）", () => {
    expect(SRC).toContain('data-testid="tasks-overview-loading"');
    expect(SRC).toContain('data-testid="tasks-overview-load-error"');
    expect(SRC).toMatch(/const showStale = loaded && !fetchOk/);
    expect(SRC).toMatch(/const neverLoaded = !loaded && !firstLoading/);
    // 失败/过期两处都有重试按钮，落 refreshTasksOverview
    expect(SRC.match(/t\("retry"\)/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("schedule 行：next_fire_at（epoch 秒）按本地语言/时区格式化，且与周期 detail 同显", () => {
    expect(SRC).toContain("formatNextFireAt(h.next_fire_at, locale)");
    expect(SRC).toContain("const locale = useLocale();");
    expect(SRC).toMatch(/fireLabel && h\.detail && <span className="mx-1">·<\/span>/);
    // 旧的字符串拼接展示不得残留
    expect(SRC).not.toContain('t("nextFire", { at: h.next_fire_at })');
  });

  it("停止动线：确认后走 stopHandleWithRollback（乐观移除+失败回滚），错误落 stopError 条", () => {
    expect(SRC).toContain("stopHandleWithRollback(h)");
    expect(SRC).toContain('t("stopFailed", { title: h.title })');
    expect(SRC).toContain('data-testid="tasks-overview-stop-error"');
  });

  it("标题导航 → openHandle：run→workflow 面板、schedule→/scheduled、其余→open_session 三步", () => {
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

  it("e2e 定位点：面板/行/停止按钮 testid（与 packaged UI e2e 契约冻结）", () => {
    expect(SRC).toContain('data-testid="tasks-overview-panel"');
    expect(SRC).toContain('data-testid={`tasks-overview-item-${h.kind}`}');
    expect(SRC).toContain('data-testid={`tasks-overview-stop-${h.kind}`}');
  });
});

describe("RightPanel / 入口接线", () => {
  it("RightPanel：running tab 挂载面板 + 总徽标（徽标走并行落地的轻量 tasksCounts hub，面板明细仍走 tasksOverview）", () => {
    expect(RIGHT_PANEL).toContain('import { TasksOverviewPanel } from "./TasksOverviewPanel"');
    expect(RIGHT_PANEL).toContain('{tab === "running" && <TasksOverviewPanel />}');
    // 2026-11-21 性能项（并行子代理）：徽标改走 lib/tasksCounts 轻量订阅，
    // 本契约只钉「徽标活着 + 面板挂载」两点。
    expect(RIGHT_PANEL).toContain("subscribeTasksCounts");
    expect(RIGHT_PANEL).toMatch(/meta\.id === "running" && runningTotal > 0/);
  });

  it("徽标 tooltip/aria 携带三档 breakdown：不进面板就能分清执行中/等你处理/已排期（旧 sidecar 无 phases 退回纯总数）", () => {
    expect(RIGHT_PANEL).toContain("tb(\"runningBreakdown\"");
    expect(RIGHT_PANEL).toContain("runningPhases");
    expect(RIGHT_PANEL).toMatch(/tb\("running", \{ n: runningTotal \}\)/);
  });

  it("侧栏品牌行 ▶N 徽标已按用户要求移除（2026-11-21）——入口收敛到右栏 tab 与 menubar，绝不能重加", () => {
    expect(SIDEBAR_HEADER).not.toContain('data-testid="sidebar-running-badge"');
    expect(SIDEBAR_HEADER).not.toContain("sidebar-running-badge");
    expect(SIDEBAR_HEADER).not.toContain("▶");
  });

  it("AppShell 注册 __ginnoOpenTasksOverview 桥（menubar ▶N 下拉「查看全部」的落点）", () => {
    expect(APP_SHELL).toContain("} = useShellActivity(g, router, pathname);");
    expect(SHELL_ACTIVITY).toContain("openTasksOverview = () => {");
    expect(SHELL_ACTIVITY).toContain(".__ginnoOpenTasksOverview =");
    expect(SHELL_ACTIVITY).toContain('setRightTab("running")');
  });
});

describe("i18n：right.json 双语键齐全", () => {
  const zh = (zhRight.right as Record<string, unknown>)["tasksOverview"] as Record<string, unknown>;
  const en = (enRight.right as Record<string, unknown>)["tasksOverview"] as Record<string, unknown>;

  it("tabs.running / badges.running / badges.runningBreakdown 双语", () => {
    expect((zhRight.right.tabs as Record<string, string>)["running"]).toBe("运行");
    expect((enRight.right.tabs as Record<string, string>)["running"]).toBe("Running");
    expect((zhRight.right.badges as Record<string, string>)["running"]).toContain("{n}");
    expect((enRight.right.badges as Record<string, string>)["running"]).toContain("{n}");
    for (const key of ["running", "waiting", "scheduled"]) {
      expect((zhRight.right.badges as Record<string, string>)["runningBreakdown"]).toContain(
        `{${key}}`,
      );
      expect((enRight.right.badges as Record<string, string>)["runningBreakdown"]).toContain(
        `{${key}}`,
      );
    }
  });

  it("三档 phases 键双语（执行中/等你处理/已排期）+ 六组 groups/stopBtn/confirmTitle 键", () => {
    for (const p of PHASES) {
      expect((zh.phases as Record<string, string>)[p]).toBeTruthy();
      expect((en.phases as Record<string, string>)[p]).toBeTruthy();
    }
    expect(zh.phases).toEqual({
      running: "执行中",
      waiting: "等你处理",
      scheduled: "已排期",
    });
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
      "loading",
      "firstLoadFailed",
      "retry",
      "nextFire",
      "external",
      "notStoppable",
      "openTitle",
      "stopFailed",
      "confirmBodyPause",
      "confirmBodyIrreversible",
      "badgeEntry",
    ]) {
      expect(typeof zh[key]).toBe("string");
      expect(typeof en[key]).toBe("string");
    }
    // 「查看」按钮已退役：view 键不得残留（无死键）
    expect(zh["view"]).toBeUndefined();
    expect(en["view"]).toBeUndefined();
  });

  it("确认文案语义：schedule=暂停可恢复（zh 提到「恢复」），其余=不可撤销（en 提到 cannot be undone）", () => {
    expect(zh.confirmBodyPause as string).toContain("恢复");
    expect(en.confirmBodyPause as string).toContain("resume");
    expect(zh.confirmBodyIrreversible as string).toContain("不可撤销");
    expect(en.confirmBodyIrreversible as string).toContain("cannot be undone");
  });

  it("stopFailed / openTitle 双语都带 {title} 占位（面板传参）", () => {
    expect(zh.stopFailed as string).toContain("{title}");
    expect(en.stopFailed as string).toContain("{title}");
    expect(zh.openTitle as string).toContain("{title}");
    expect(en.openTitle as string).toContain("{title}");
  });
});
