// 远端会话前端守卫（审计 C2）——字符串契约测试。
//
// 范式同 modelChipRemote.test.tsx：vitest node 环境无 DOM，读源码钉住
// 守卫契约；纯判定函数在 remoteGuard.test.ts 真 import 覆盖；渲染真实性
// 由 packaged UI e2e 覆盖（remote_peer 会话可由 _sidecar fixture 构造，
// 参考 test_packaged_ui_llm_events.py）。
//
// 已核实的痛点：remote 会话（session.remote_peer 非空）执行在远端节点，
// 后端转发只送 user_text——附件 / @mention / slash skill 全部静默丢弃
//（fail-loud 归 C1）。用户侧看到的是：附件按钮可点但传了没反应、@ 菜单
// 可开但 mention 无效、文件面板列恒空目录、plans 面板永远空白。本套守卫
// 堵入口 + 讲原因。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enChat from "../../../../messages/en/chat.json";
import enCode from "../../../../messages/en/code.json";
import enShell from "../../../../messages/en/shell.json";
import zhChat from "../../../../messages/zh-CN/chat.json";
import zhCode from "../../../../messages/zh-CN/code.json";
import zhShell from "../../../../messages/zh-CN/shell.json";

const COMPOSER = readFileSync(
  fileURLToPath(new URL("./Composer.tsx", import.meta.url)),
  "utf8",
);
const STREAM = readFileSync(
  fileURLToPath(new URL("../ChatStream.tsx", import.meta.url)),
  "utf8",
);
const CODE = readFileSync(
  fileURLToPath(new URL("../../right/code/CodePanel.tsx", import.meta.url)),
  "utf8",
);
const PLAN = readFileSync(
  fileURLToPath(new URL("../PlanBoard.tsx", import.meta.url)),
  "utf8",
);

describe("Composer：附件 / slash 入口禁用（发送纯文本不受影响）", () => {
  it("判定来自 remoteGuard 单一源（isRemoteSession）", () => {
    expect(COMPOSER).toContain('import { isRemoteSession } from "@/components/chat/remoteGuard"');
    expect(COMPOSER).toContain("const remoteSession = isRemoteSession(session);");
  });

  it("附件按钮 disabled + 原因 tooltip + e2e 定位点", () => {
    const btn = COMPOSER.slice(
      COMPOSER.indexOf('onClick={() => fileRef.current?.click()}'),
      COMPOSER.indexOf("<Paperclip", COMPOSER.indexOf('onClick={() => fileRef.current?.click()}')),
    );
    expect(btn).toContain("disabled={remoteSession}");
    expect(btn).toContain('data-testid={remoteSession ? "composer-attach-remote" : undefined}');
    expect(btn).toContain('tc("composer.remoteAttachTitle")');
  });

  it("slash 按钮 disabled + 原因 tooltip + e2e 定位点", () => {
    // 锚在 onClick 之后的禁用块：title 与 disabled 都在按钮开标签内
    const from = COMPOSER.indexOf('if (!input.trim()) {');
    const slash = COMPOSER.slice(from, COMPOSER.indexOf("<Keyboard", from));
    expect(slash).toContain("disabled={remoteSession}");
    expect(slash).toContain('data-testid={remoteSession ? "composer-slash-remote" : undefined}');
    expect(slash).toContain('title={remoteSession ? tc("composer.remoteSlashTitle") : tc("composer.slashTitle")}');
  });

  it("拖拽 / 粘贴文件路径同样堵住（不调 addFiles，粘贴文本不拦）", () => {
    const drop = COMPOSER.slice(COMPOSER.indexOf("onDrop={"), COMPOSER.indexOf("addFiles(e.dataTransfer.files)"));
    expect(drop).toContain("if (remoteSession) return;");
    const paste = COMPOSER.slice(COMPOSER.indexOf("onPaste={"), COMPOSER.indexOf("onKeyDown={"));
    expect(paste).toContain("if (remoteSession) return;");
    // 粘贴守卫必须在 preventDefault 之前——文本照常贴入
    expect(paste.indexOf("if (remoteSession) return;")).toBeLessThan(
      paste.indexOf("e.preventDefault();"),
    );
  });

  it("textarea 本身不禁用——纯文本发送是 remote 会话的唯一输入通道", () => {
    // 只看开标签（到第一个 '>'），onKeyDown 里的 !it.disabled 是菜单逻辑
    const open = COMPOSER.slice(COMPOSER.indexOf("<textarea"), COMPOSER.indexOf(">", COMPOSER.indexOf("<textarea")));
    expect(open).not.toContain("disabled");
    expect(open).not.toContain("readOnly");
  });

  it("运行中提示条：remote && running 显示「本轮结束作为新消息送达」", () => {
    expect(COMPOSER).toContain("{remoteSession && running && (");
    expect(COMPOSER).toContain('tc("composer.remoteSteerHint")');
  });
});

describe("ChatStream：@ mention / slash 菜单 fail-closed", () => {
  it("recomputeMenu 入口被 isRemoteSession 守卫拦下（键盘打 @ 或 / 不弹菜单）", () => {
    const fn = STREAM.slice(STREAM.indexOf("function recomputeMenu("));
    const guardIdx = fn.indexOf("if (isRemoteSession(session))");
    const buildIdx = fn.indexOf("buildMenuItems(");
    expect(guardIdx).toBeGreaterThan(-1);
    expect(guardIdx).toBeLessThan(buildIdx);
  });

  it("pickItem 同样守一道（纵深防御，同 useModelPick 范式）", () => {
    const fn = STREAM.slice(STREAM.indexOf("function pickItem("));
    const guardIdx = fn.indexOf("if (isRemoteSession(session)) return;");
    expect(guardIdx).toBeGreaterThan(-1);
  });
});

describe("CodePanel：文件面板显示归属说明而非空列表", () => {
  it("remote 时根列表过滤恒空的 session 工作区根（本地挂载保留）", () => {
    expect(CODE).toContain('remoteSession ? roots.filter((r) => r.id !== "session") : roots');
  });

  it("RootBar 渲染 displayRoots（切换器不再出现恒空根）", () => {
    expect(CODE).toContain("roots={displayRoots}");
  });

  it("残留选中 session 根时 effectiveRootId 回退本地根，不对其发请求", () => {
    expect(CODE).toContain("displayRoots.some((r) => r.id === codeRootId)");
    expect(CODE).toContain("displayRoots[0]?.id ?? null");
  });

  it("顶部说明条（panel.remoteNote，带 peer 名）+ e2e 定位点", () => {
    expect(CODE).toContain('data-testid="code-panel-remote-note"');
    expect(CODE).toContain('t("panel.remoteNote", { name: remotePeerDisplayName(activeSessionMeta) })');
  });
});

describe("PlanBoard：plans 面板显示对端管理说明", () => {
  it("空 plans 且 remote 渲染说明卡，本地空 plans 仍返回 null（零影响）", () => {
    const fn = PLAN.slice(PLAN.indexOf("if (plans.length === 0)"));
    expect(fn).toContain("if (!remotePeer) return null;");
    expect(PLAN).toContain('data-testid="plan-board-remote-note"');
    expect(PLAN).toContain('t("planBoard.remoteNote", { name: remotePeerDisplayName(remoteSessionMeta) })');
  });
});

describe("i18n：双语齐全、嵌套 key、带 {name} 占位", () => {
  const chatKeys = [
    "composer.remoteAttachTitle",
    "composer.remoteSlashTitle",
    "composer.remoteSteerHint",
    "planBoard.remoteNote",
  ];

  it.each(chatKeys)("chat.%s 双语存在且已翻译", (key) => {
    const [section, leaf] = key.split(".");
    const zhSec = zhChat.chat as unknown as Record<string, Record<string, unknown>>;
    const enSec = enChat.chat as unknown as Record<string, Record<string, unknown>>;
    const zh = zhSec[section]?.[leaf];
    const en = enSec[section]?.[leaf];
    expect(typeof zh).toBe("string");
    expect(typeof en).toBe("string");
    expect(zh).not.toBe(en);
    // key 嵌套（对象分层），不是带点扁平 key——IntlError 教训
    expect(zhSec[section]).toBeDefined();
  });

  it("planBoard.remoteNote 与 code.panel.remoteNote 带 {name} 占位", () => {
    const zhPlan = (zhChat.chat as unknown as Record<string, Record<string, string>>).planBoard;
    const enPlan = (enChat.chat as unknown as Record<string, Record<string, string>>).planBoard;
    const zhPanel = (zhCode.code as unknown as Record<string, Record<string, string>>).panel;
    const enPanel = (enCode.code as unknown as Record<string, Record<string, string>>).panel;
    expect(zhPlan.remoteNote).toContain("{name}");
    expect(enPlan.remoteNote).toContain("{name}");
    expect(zhPanel.remoteNote).toContain("{name}");
    expect(enPanel.remoteNote).toContain("{name}");
  });

  it("code.panel.remoteNote 双语齐全", () => {
    const zh = (zhCode.code as unknown as Record<string, Record<string, string>>).panel.remoteNote;
    const en = (enCode.code as unknown as Record<string, Record<string, string>>).panel.remoteNote;
    expect(zh).toContain("对端节点");
    expect(zh).not.toBe(en);
  });

  it("TopBar 徽标 tooltip 补「对端的模型与记忆体系」句", () => {
    const zhS = zhShell.shell as unknown as Record<string, string>;
    const enS = enShell.shell as unknown as Record<string, string>;
    expect(zhS["remote-badge-title"]).toContain("记忆体系");
    expect(zhS["remote-badge-title-noendpoint"]).toContain("记忆体系");
    expect(enS["remote-badge-title"]).toContain("memory");
    expect(enS["remote-badge-title-noendpoint"]).toContain("memory");
  });
});
