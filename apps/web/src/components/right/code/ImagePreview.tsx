"use client";

/**
 * Read-only image preview for the code panel (design §4.5's degradation chain:
 * "图片走 <img>"). The runtime flags image files with
 * `readonly_reason: "image"` and this streams their bytes from `/api/code/raw`
 * — the JSON clients cannot be used here, because a `{ok:false}` body would
 * render as a corrupt picture rather than as a failure.
 *
 * SVG comes here too. Its bytes are text, but its content is a picture, so the
 * runtime classifies it as `image` BEFORE the binary sniff rather than letting
 * it fall through to the editor as markup. Served as `image/svg+xml` inside an
 * `<img>` its script is inert, which is why no sandboxing is needed here — we
 * deliberately offer no inline/iframe surface (design §9.3).
 */

import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import { cn } from "@/lib/utils";

export function ImagePreview({
  path,
  url,
  className,
}: {
  path: string;
  url: string;
  className?: string;
}) {
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [failed, setFailed] = useState(false);

  // A new url means a new file — the url carries `v=<version>`, so a rewrite
  // changes it. Reset both, or a failed load would stick to the next file.
  useEffect(() => {
    setDims(null);
    setFailed(false);
  }, [url]);

  const name = path.split("/").pop() ?? path;

  if (failed) {
    return (
      <div
        className={cn(
          "flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint",
          className,
        )}
      >
        <ImageOff size={18} />
        <span>无法显示这张图片</span>
        <span className="text-[11px] text-faint/80">
          文件可能过大（上限 32 MB）、已损坏，或不在可预览的格式内
        </span>
      </div>
    );
  }

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-panel", className)}>
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-4">
        {/* `alt` carries the filename, so the broken-image fallback and a
            screen reader both say which file this is. */}
        <img
          src={url}
          alt={name}
          onLoad={(e) =>
            setDims({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
          }
          onError={() => setFailed(true)}
          className="max-h-full max-w-full object-contain"
        />
      </div>
      <div className="flex shrink-0 items-center gap-2 border-t border-line px-3 py-1.5 text-[11px] text-faint">
        <span className="truncate">{name}</span>
        {dims ? (
          <span className="tabular-nums">
            {dims.w} × {dims.h}
          </span>
        ) : null}
        <span className="ml-auto shrink-0">只读预览</span>
      </div>
    </div>
  );
}