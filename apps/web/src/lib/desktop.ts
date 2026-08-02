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
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
