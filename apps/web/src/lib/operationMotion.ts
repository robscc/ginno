export const OPERATION_MOTION_KEY = "ginno-operation-motion";

export function readOperationMotion(): boolean {
  try {
    return localStorage.getItem(OPERATION_MOTION_KEY) !== "0";
  } catch {
    return true;
  }
}

export function applyOperationMotion(enabled: boolean, persist = true): void {
  if (typeof document === "undefined") return;
  document.documentElement.toggleAttribute("data-operation-motion-paused", !enabled);
  if (!persist) return;
  try {
    localStorage.setItem(OPERATION_MOTION_KEY, enabled ? "1" : "0");
  } catch {
    // The current window can still apply the preference when storage is unavailable.
  }
}
