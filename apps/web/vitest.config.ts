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
      "@": path.resolve(__dirname, "."),
    },
  },
});
