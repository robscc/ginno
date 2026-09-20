import { PinApp } from "@/components/pin/PinApp";

/**
 * /pin — the floating quick-chat window's only route
 * (docs/floating-window-design.md §2). Next static-export emits pin.html,
 * which the sidecar's `_serve_web` matches via its `p + ".html"` fallback and
 * the Tauri shell loads in the "pin" WebviewWindow. AppShell detects this
 * pathname and renders children bare (no sidebar/chrome).
 */
export default function PinPage() {
  return <PinApp />;
}
