/**
 * EnvVarsSettings 渲染冒烟（node 环境，无 DOM）：
 * react-dom/server 渲染初始「加载中」态——验证行/保存按钮锁定（加载未完成
 * 不可写）与验收选择器齐全。交互态（ready/failed/corrupt/冲突保草稿/
 * 敏感替换）依赖 useEffect + DOM 事件，node 环境跑不了，按仓库分工留给
 * packaged UI e2e（本任务提供选择器与人工步骤，不在此重复）。
 *
 * CAS 版（settings-integrity）：视图走 GET /api/settings/env、保存走
 * PATCH /api/settings/env——桩换成 env 专用端点。
 */
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string) => k,
}));
vi.mock("@/lib/runtime", () => ({
  getEnvSettings: () => new Promise(() => {}), // 永不 resolve：稳定停在 loading 态
  patchEnvSettings: async () => ({ ok: true, view: { status: "ok", entries: {} } }),
}));

import { EnvVarsSettings } from "./EnvVarsSettings";

describe("EnvVarsSettings 初始渲染（loading 态）", () => {
  const html = renderToStaticMarkup(<EnvVarsSettings />);

  it("验收选择器齐全：容器/状态/保存按钮", () => {
    expect(html).toContain('data-testid="env-vars"');
    expect(html).toContain('data-testid="env-status"');
    expect(html).toContain('data-testid="env-save"');
    expect(html).toContain('data-testid="env-add"');
  });

  it("加载未完成：保存按钮禁用", () => {
    expect(html).toMatch(/data-testid="env-save"[^>]*disabled/);
  });

  it("状态行显示 loading 文案、无重试按钮（loadFailed 才出现）", () => {
    expect(html).toContain("loading");
    expect(html).not.toContain('data-testid="env-retry"');
  });
});
