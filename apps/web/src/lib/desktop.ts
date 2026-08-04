/**
 * Tauri shell detection — dependency-free (no @tauri-apps/api needed).
 *
 * The only behavioral fork today is file saving: WKWebView can't trigger
 * browser downloads (anchor[download] on a blob URL is a no-op), so the
 * desktop UI asks the sidecar to copy the file into ~/Downloads instead
 * (see runtime.saveFileToDownloads). Plain browsers keep the native
 * download flow.
 */

export function isDesktop(): boolean {
  if (typeof window === "undefined") return false;
  if ("__TAURI_INTERNALS__" in window) return true;
  // Release-build fallback: the packaged webview may arrive at this origin via
  // the splash page's location.replace (see apps/desktop/src/lib.rs), in which
  // case the Tauri bridge may not be present. Detect WKWebView directly — its
  // user agent contains AppleWebKit but neither a Safari nor a Chrome token.
  const ua = navigator.userAgent;
  return ua.includes("AppleWebKit") && !ua.includes("Safari") && !ua.includes("Chrome");
}
