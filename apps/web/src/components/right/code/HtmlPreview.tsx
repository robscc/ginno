"use client";

/**
 * Rendered-page preview for html/htm files in the code panel (the design §1.3
 * "no HTML sandbox rendering" decision, reversed 2026-10-21 — see the row in
 * the table).
 *
 * The document streams from `/api/code/file/...` (path-shaped, so relative
 * sub-resources resolve) and is sandboxed TWICE, on purpose: the iframe
 * attribute here AND a CSP `sandbox allow-scripts; connect-src 'none'` header
 * the server puts on every response (code.py `_CSP_TMPL`). No
 * `allow-same-origin` → the page runs on an opaque origin: it cannot touch the
 * app's DOM or storage, and CSP takes away fetch/XHR/WebSocket so it cannot
 * reach the runtime API either (the app's CORS is `allow_origins=["*"]`, which
 * is exactly why the CSP half is not optional).
 *
 * The fetch-then-frame order is deliberate: a failed iframe load would render
 * the server's JSON error body as page text with no signal we can read, so we
 * check reachability first and render our own failure state (like ImagePreview
 * does for corrupt pictures).
 */

import { useEffect, useState } from "react";
import { EyeOff, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export function HtmlPreview({
  path,
  url,
  className,
}: {
  path: string;
  url: string;
  className?: string;
}) {
  const t = useTranslations("code.preview");
  const [phase, setPhase] = useState<"checking" | "ok" | "failed">("checking");
  const [failedCode, setFailedCode] = useState<string>("");
  // A manual bump re-runs the check and rebuilds the iframe — the panel-level
  // reload button refreshes the READ, but an unchanged file keeps its version
  // (and therefore this component's key), so the preview needs its own reload.
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    setPhase("checking");
    let alive = true;
    fetch(url)
      .then((r) => {
        if (!alive) return;
        if (r.ok) {
          setPhase("ok");
        } else {
          setFailedCode(r.headers.get("X-Ginno-Code") ?? "");
          setPhase("failed");
        }
      })
      .catch(() => {
        if (alive) setPhase("failed");
      });
    return () => {
      alive = false;
    };
  }, [url, nonce]);

  const name = path.split("/").pop() ?? path;

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-panel", className)}>
      <div className="min-h-0 flex-1">
        {phase === "ok" ? (
          /* The iframe attribute is the second half of the sandbox — the
             response's CSP is the first. allow-scripts only: no same-origin,
             no forms, no popups, no top navigation. */
          <iframe
            key={nonce}
            src={url}
            sandbox="allow-scripts"
            title={name}
            className="h-full w-full border-0 bg-white"
          />
        ) : phase === "checking" ? null : (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint">
            <EyeOff size={18} />
            <span>{t("failed")}</span>
            <span className="text-[11px] text-faint/80">{t("failedHint")}</span>
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2 border-t border-line px-3 py-1.5 text-[11px] text-faint">
        <span className="truncate">{name}</span>
        <span className="ml-auto shrink-0">{t("sandboxed")}</span>
        <button
          type="button"
          title={t("reload")}
          onClick={() => setNonce((n) => n + 1)}
          className="shrink-0 rounded p-0.5 hover:bg-line"
        >
          <RotateCcw size={12} />
        </button>
      </div>
    </div>
  );
}
