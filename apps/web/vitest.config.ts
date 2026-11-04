import { defineConfig } from "vitest/config";
import path from "node:path";

// Node-environment unit tests for pure-logic modules (no React/DOM yet).
// Keep new tests dependency-free: import the module, assert on plain values.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    // 覆盖率（CI 上传 Codecov 用，本地跑 pnpm test 依旧轻快）：
    // v8 provider + cobertura XML 报告（istanbul-reports 3.2 里 xml 格式的
    // 名字是 cobertura，没有独立的 "xml" reporter），产物在 coverage/ 下
    // （已 gitignore）。
    coverage: {
      provider: "v8",
      reporter: ["text", "cobertura"],
      include: ["src/**"],
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
