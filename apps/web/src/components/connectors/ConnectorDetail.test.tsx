/* ConnectorDetail 未保存离开守卫的回归测试（node 环境，照 A2aInboundCard.test.tsx
 * 的源码断言风格——无 DOM，不渲染组件）。
 *
 * 事故（2026-11-16）：用户进 A2A 连接器后**切不到任何其他页面**，被困在详情页。
 * 根因：canLeave() 同步调 window.confirm，而 Tauri 的 WKWebView 屏蔽原生 JS
 * 对话框、confirm() **恒返回 false** → canLeave() 恒 false → 关闭与切 tab 全被
 * 静默拒绝。仓库里早有 ConfirmModal 与 AgentsSettings 的先例注释，此处漏用。
 *
 * 这些用例钉住「不得回退到原生 confirm」，并校验新 i18n 键双语齐全。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enConn from "../../../messages/en/conn.json";
import zhConn from "../../../messages/zh-CN/conn.json";

const SRC = readFileSync(
  fileURLToPath(new URL("./ConnectorDetail.tsx", import.meta.url)),
  "utf8",
);

describe("ConnectorDetail 未保存离开守卫", () => {
  it("不得用原生 window.confirm（WKWebView 恒 cancel → 用户被困页面）", () => {
    // 只查真实调用，注释里提到 window.confirm 是允许的（本文件与源码注释都在解释它）
    const calls = [...SRC.matchAll(/(?<!\/\/[^\n]*)\bwindow\.confirm\s*\(/g)];
    expect(
      calls.length,
      "ConnectorDetail 仍在调 window.confirm——Tauri 下恒 false，会把用户困在详情页",
    ).toBe(0);
  });

  it("改用应用内 ConfirmModal 承载确认", () => {
    expect(SRC).toContain('import { ConfirmModal } from "@/components/ConfirmModal"');
    expect(SRC).toMatch(/<ConfirmModal\b/);
  });

  it("守卫是挂起动作而非同步布尔（guardLeave + pendingLeave）", () => {
    expect(SRC).toMatch(/const\s+guardLeave\s*=\s*useCallback/);
    expect(SRC).toMatch(/setPendingLeave\(/);
    // 关闭与切 tab 都必须走守卫，不能绕过
    expect(SRC).toMatch(/const requestClose = useCallback\(\(\) => \{\s*guardLeave\(onClose\)/);
    expect(SRC).toMatch(/guardLeave\(\(\) => \{/);
  });

  it("dirty 时才挂起；干净时直接执行（不弹无谓模态）", () => {
    const m = SRC.match(/const guardLeave = useCallback\(\(action: \(\) => void\) => \{([\s\S]*?)\n  \}, \[\]\);/);
    expect(m, "未找到 guardLeave 实现").toBeTruthy();
    const body = m![1];
    expect(body).toContain("if (!dirtyRef.current)");
    expect(body).toMatch(/action\(\);\s*return;/);
    expect(body).toContain("setPendingLeave(() => action)");
  });

  it("确认后清 dirty 再执行动作（否则下次离开又被拦）", () => {
    const m = SRC.match(/onConfirm=\{\(\) => \{([\s\S]*?)\n            \}\}/);
    expect(m, "未找到 ConfirmModal.onConfirm").toBeTruthy();
    const body = m![1];
    expect(body).toContain("setPendingLeave(null)");
    expect(body).toContain("dirtyRef.current = false");
    expect(body).toMatch(/action\(\);/);
  });

  it("ESC 在确认模态开着时只取消模态，不连带关抽屉", () => {
    const m = SRC.match(/if \(e\.key !== "Escape"\) return;([\s\S]*?)window\.addEventListener/);
    expect(m, "未找到 ESC 处理").toBeTruthy();
    const body = m![1];
    const idxPending = body.indexOf("if (pendingLeave)");
    const idxWizard = body.indexOf("if (wizardOpen)");
    expect(idxPending).toBeGreaterThan(-1);
    expect(idxWizard).toBeGreaterThan(-1);
    expect(idxPending, "pendingLeave 分支必须早于 wizardOpen").toBeLessThan(idxWizard);
    // effect 依赖必须含 pendingLeave，否则闭包拿到旧值
    expect(SRC).toMatch(/\}, \[requestClose, wizardOpen, storeOpen, pendingLeave\]\);/);
  });

  it("新增 i18n 键 en/zh-CN 双语齐全且非空", () => {
    const enCfg = (enConn as Record<string, any>).conn.config;
    const zhCfg = (zhConn as Record<string, any>).conn.config;
    for (const k of ["unsavedConfirm", "unsavedTitle", "discardAndLeave"]) {
      expect(enCfg[k], `en 缺 conn.config.${k}`).toBeTruthy();
      expect(zhCfg[k], `zh 缺 conn.config.${k}`).toBeTruthy();
    }
  });

  it("组件里 tr(\"config.…\") 字面量 key 全部存在于双语 catalog", () => {
    const keys = [...SRC.matchAll(/\btr\("config\.([a-zA-Z0-9]+)"/g)].map((m) => m[1]);
    expect(keys.length, "tr(\"config.…\") 扫描为空说明正则失效").toBeGreaterThan(0);
    const enCfg = (enConn as Record<string, any>).conn.config;
    const zhCfg = (zhConn as Record<string, any>).conn.config;
    for (const k of keys) {
      expect(enCfg[k], `en 缺 conn.config.${k}`).toBeTruthy();
      expect(zhCfg[k], `zh 缺 conn.config.${k}`).toBeTruthy();
    }
  });
});
