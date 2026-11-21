import { defineConfig } from "vitest/config";
import path from "node:path";

// Node-environment unit tests for pure-logic modules (no React/DOM yet).
// Keep new tests dependency-free: import the module, assert on plain values.
export default defineConfig({
  // JSX 走 automatic runtime（react/jsx-runtime）：组件级测试渲染 .tsx 时
  // esbuild 默认 classic transform 会要求 React 在作用域内，而组件按 Next
  // 约定不 import React——只影响含 JSX 的测试文件，纯逻辑测试不受影响。
  esbuild: { jsx: "automatic" },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    // 覆盖率（CI 上传 Codecov 用，本地跑 pnpm test 依旧轻快）：
    // v8 provider + cobertura XML 报告（istanbul-reports 3.2 里 xml 格式的
    // 名字是 cobertura，没有独立的 "xml" reporter），产物在 coverage/ 下
    // （已 gitignore）。
    //
    // ⚠️ 口径：只统计 src/lib/**（纯逻辑层），**不是** src 全量。
    //
    // 为什么收窄（2026-11-15 覆盖率调查）：本配置是 environment: "node"，
    // 无 DOM，且仓库刻意不装 jsdom / @testing-library（见下方「分工」）。
    // 原先 include: ["src/**"] 把 321 个源文件全计入分母，其中
    // 237 个 React 组件 + 12 个 Next 页面（77.6%）在本环境下**结构上不可测**，
    // 于是 All files 恒为 4.91% —— 这个数字既不代表质量也不代表缺口，
    // 只会误导决策（曾据此误判「覆盖率一直偏低」，而 runtime 实为 81%）。
    //
    // 分工：**渲染层（组件/页面的 DOM 交互）由 packaged UI e2e 覆盖**
    // （packages/runtime/tests/e2e/test_packaged_ui_*.py，真 dist + 真 Chromium；
    // CLAUDE.md 立规：前端交互改动必须配 packaged UI 用例）。
    // e2e 的覆盖不计入 vitest 报表，所以把组件放进分母只会永久摊薄数字。
    // 组件里抽出的纯逻辑函数仍应放 src/lib 或就地 .test.ts 测纯函数部分。
    //
    // 若将来引入组件渲染测试（jsdom + testing-library），再把 include 放宽
    // 回 src/**，并同时调整阈值——两者必须同批改，否则门禁立刻假红。
    coverage: {
      provider: "v8",
      reporter: ["text", "cobertura"],
      include: ["src/lib/**"],
      // 回归门禁（2026-11-15 第二批补测落地后上调；**只升不降**）。
      // 第一批后实测 46.32% → 阈值 44/44/88/45；
      // 第二批后实测 lines/statements 69.29% · branches 95.56% · functions 93.11%
      // → 上调为 66/66/92/90，仍留约 3pp 边际。
      // 阈值刻意不贴当前值：目的是**防回归**，不是冲指标——
      // 贴着设会因 CI 环境差异/新增未测文件而假红。
      // 抬阈值的前提是先补测，且必须与 include 口径同批调整。
      thresholds: {
        lines: 66,
        statements: 66,
        branches: 92,
        functions: 90,
      },
    },
  },
  resolve: {
    alias: {
      // 与 tsconfig paths 同口径（@/* → src/*）。曾错指仓库根：纯相对导入
      // 的测试碰不到别名，一旦测试链拉进 provider.tsx（@/lib/runtime）即
      // Cannot find module（2026-11-05 notifyHub 测试首现）。
      "@": path.resolve(__dirname, "src"),
    },
  },
});
