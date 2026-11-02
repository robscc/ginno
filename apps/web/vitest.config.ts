import { defineConfig } from "vitest/config";
import path from "node:path";

// Node-environment unit tests for pure-logic modules (no React/DOM yet).
// Keep new tests dependency-free: import the module, assert on plain values.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
});
