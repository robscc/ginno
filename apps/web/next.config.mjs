/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export — served by Tauri webview. No SSR.
  output: "export",
  // Tauri dev serves from :3000, production loads the static files.
  images: { unoptimized: true },
  // Strip the trailing `.html` from routes when served by Tauri.
  trailingSlash: false,
  // Allow absolute URLs to the sidecar from the webview.
  reactStrictMode: true,
};

export default nextConfig;
