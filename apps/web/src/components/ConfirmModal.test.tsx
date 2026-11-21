/* ConfirmModal 焦点/键盘可达性回归测试（node 环境，照 ConnectorDetail.test.tsx
 * 的源码断言风格——无 DOM，不渲染组件；真实浏览器行为另有一次性 Playwright
 * 验证台核对，见对应报告）。
 *
 * 背景（2026-11-21）：ConfirmModal 原先只有 role=dialog 和遮罩点击关闭，
 * 没有焦点管理——打开后焦点仍停在触发按钮上，Tab 会跑到模态底下的页面内容，
 * Esc 无人处理（底下面板的 Esc 监听先到，一次按键连环关掉抽屉），关闭后
 * 焦点也不回触发器。本文件钉住补齐后的行为契约。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./ConfirmModal.tsx", import.meta.url)),
  "utf8",
);

describe("ConfirmModal 对外 API 兼容（现有 20 处调用零改动）", () => {
  it("props 签名保持 title/message/confirmLabel/onConfirm/onCancel", () => {
    const m = SRC.match(/export function ConfirmModal\(\{([\s\S]*?)\}: \{/);
    expect(m, "未找到 ConfirmModal 函数签名").toBeTruthy();
    const props = [...m![1].matchAll(/\b(\w+),/g)].map((x) => x[1]);
    expect(props).toEqual([
      "title",
      "message",
      "confirmLabel",
      "onConfirm",
      "onCancel",
    ]);
  });

  it("确认键默认仍取 ui.common.delete，取消键仍取 ui.common.cancel", () => {
    expect(SRC).toContain('t("common.delete")');
    expect(SRC).toContain('t("common.cancel")');
  });

  it("覆盖层 mousedown 关闭 + 面板内 stopPropagation 的既有行为不变", () => {
    expect(SRC).toMatch(/onMouseDown=\{onCancel\}/);
    expect(SRC).toMatch(/onMouseDown=\{\(e\) => e\.stopPropagation\(\)\}/);
  });

  it("不回退到原生对话框（Tauri WKWebView 不可靠，见 ConnectorDetail 事故）", () => {
    // 只匹配真实调用（docstring 里提到 window.confirm 的解释性文字不算）
    expect(SRC).not.toMatch(
      /window\.confirm\s*\(|window\.prompt\s*\(|window\.alert\s*\(|\.showModal\s*\(/,
    );
  });
});

describe("ConfirmModal 初始焦点：默认聚焦安全动作「取消」", () => {
  it("挂载时聚焦取消按钮", () => {
    expect(SRC).toMatch(/cancelBtnRef\.current\?\.focus\(\)/);
    // 确认按钮不得抢初始焦点
    expect(SRC).not.toMatch(/autoFocus/);
  });

  it("两个按钮都有可见焦点环（focus-visible outline）", () => {
    const hits = SRC.match(/focus-visible:outline-accent/g);
    expect(hits?.length ?? 0).toBeGreaterThanOrEqual(2);
  });
});

describe("ConfirmModal Esc：栈顶响应且不外泄到底层处理器", () => {
  it("Esc 在 window 捕获阶段拦截并 stopPropagation（底下的冒泡监听收不到）", () => {
    expect(SRC).toMatch(/window\.addEventListener\("keydown", onKey, true\)/);
    const m = SRC.match(/if \(e\.key === "Escape"\) \{([\s\S]*?)entry\.cancel\(\)/);
    expect(m, "未找到 Escape 处理分支").toBeTruthy();
    expect(m![1]).toContain("e.preventDefault()");
    expect(m![1]).toContain("e.stopPropagation()");
  });

  it("模块级栈：叠层时只有栈顶响应，卸载时正确出栈", () => {
    expect(SRC).toMatch(/openModals\.push\(entry\)/);
    expect(SRC).toMatch(
      /openModals\[openModals\.length - 1\] !== entry\) return;/,
    );
    expect(SRC).toMatch(/openModals\.indexOf\(entry\)[\s\S]{0,80}splice/);
  });
});

describe("ConfirmModal Tab 陷阱：焦点循环限于对话框", () => {
  it("末尾 Tab 绕回头部、首部 Shift+Tab 绕回末尾", () => {
    expect(SRC).toMatch(/active === last/);
    expect(SRC).toMatch(/active === first/);
    // 劫持默认行为必须 preventDefault + stopPropagation 成对出现（Tab 分支内）
    const tabM = SRC.match(/if \(e\.key !== "Tab"\) return;([\s\S]*?)window\.addEventListener/);
    expect(tabM, "未找到 Tab 陷阱分支").toBeTruthy();
    expect(tabM![1]).toContain("e.preventDefault()");
    expect(tabM![1]).toContain("e.stopPropagation()");
  });

  it("焦点逃出容器时拉回对话框内（而不是放任 Tab 到页面背景）", () => {
    expect(SRC).toMatch(/container\.contains\(active\)/);
  });

  it("可聚焦元素筛选排除禁用态与 tabindex=-1", () => {
    const m = SRC.match(/const FOCUSABLE_SELECTOR =([\s\S]*?);/);
    expect(m, "未找到 FOCUSABLE_SELECTOR").toBeTruthy();
    expect(m![1]).toContain("button:not([disabled])");
    expect(m![1]).toContain('[tabindex]:not([tabindex="-1"])');
  });
});

describe("ConfirmModal 关闭后回焦触发器（触发器消失合理降级）", () => {
  it("卸载时回焦打开前的元素，且以 isConnected 守卫降级", () => {
    const m = SRC.match(/const trigger = triggerRef\.current;([\s\S]*?)trigger\.focus\(\)/);
    expect(m, "未找到卸载回焦逻辑").toBeTruthy();
    expect(m![1]).toContain("isConnected");
  });
});

describe("ConfirmModal 无障碍语义", () => {
  it("role=dialog + aria-modal + labelledby/describedby（标题与正文可被读屏关联）", () => {
    expect(SRC).toMatch(/role="dialog"/);
    expect(SRC).toMatch(/aria-modal="true"/);
    expect(SRC).toMatch(/aria-labelledby=\{titleId\}/);
    expect(SRC).toMatch(/aria-describedby=\{descId\}/);
  });
});
