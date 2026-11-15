/** @type {import('next').NextConfig} */
import { readFileSync } from "node:fs";

// 版本号单一真源：package.json（发版 runbook 已 bump 的三处之一）。
// 构建期注入，避免前端再硬编码一份而漂移——历史上 mcpAppBridge 的
// hostInfo.version 长期停在 0.1.0，而实际已 0.3.3。
const pkg = JSON.parse(
  readFileSync(new URL("./package.json", import.meta.url), "utf8"),
);

const nextConfig = {
  env: {
    NEXT_PUBLIC_APP_VERSION: pkg.version,
  },
  // Static export — served by Tauri webview. No SSR.
  output: "export",
  // Tauri dev serves from :3000, production loads the static files.
  images: { unoptimized: true },
  // Strip the trailing `.html` from routes when served by Tauri.
  trailingSlash: false,
  // Allow absolute URLs to the sidecar from the webview.
  reactStrictMode: true,
  // Dev: the Tauri webview (tauri.localhost) and 127.0.0.1 hit the dev server
  // cross-origin; whitelist them so /_next assets load.
  allowedDevOrigins: ["127.0.0.1", "localhost", "tauri.localhost"],
};

export default nextConfig;
