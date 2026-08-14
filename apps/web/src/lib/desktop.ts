/**
 * Tauri shell detection + native bridges.
 *
 * Detection is dependency-free; the only behavioral forks today are file
 * saving (WKWebView can't trigger browser downloads — anchor[download] on a
 * blob URL is a no-op — so the desktop UI asks the sidecar to copy the file
 * into ~/Downloads instead, see runtime.saveFileToDownloads) and native
 * notifications (WKWebView has no window.Notification — the shell fires real
 * macOS notifications via tauri-plugin-notification, see notifyNative below).
 */

import { emit } from "@tauri-apps/api/event";

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

/** Payload for the shell-side native notification (apps/desktop/src/lib.rs). */
export interface NativeNotification {
  /** Click target kind: focus a chat session, open the Workflow panel, or
   *  "test" (Settings → Notifications test button) which only refocuses. */
  kind: "session" | "workflow-run" | "test";
  /** Session id (kind=session) or run id (kind=workflow-run). */
  id: string;
  title: string;
  body: string;
  /** macOS system sound name ("Glass", "Ping", …); omitted = silent. The
   *  shell validates against its own allow-list. */
  sound?: string;
}

/**
 * Ask the Tauri shell to fire a native macOS notification.
 *
 * Returns true when the event was handed to the shell. Returns false when not
 * running in Tauri or when the bridge is unavailable (e.g. the splash-page
 * location.replace path) — callers should fall back to the HTML5 Notification
 * API, which covers plain-browser dev (it is a no-op inside WKWebView anyway).
 */
export async function notifyNative(n: NativeNotification): Promise<boolean> {
  if (!isDesktop()) return false;
  try {
    await emit("ginno:notify", n);
    return true;
  } catch {
    return false;
  }
}

/** Tile CSS rect for the CEF hole-punch host (M2). Rust only stores geometry. */
export interface BrowserTileRect {
  x: number;
  y: number;
  width: number;
  height: number;
  space?: string | null;
  visible?: boolean;
  /** Forward WKWebView hits into the CEF hole. Geometry only — not a Space owner. */
  passthrough?: boolean;
}

export async function emitBrowserTile(rect: BrowserTileRect): Promise<boolean> {
  if (!isDesktop()) return false;
  try {
    await emit("ginno:browser-tile", rect);
    return true;
  } catch {
    return false;
  }
}
