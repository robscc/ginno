"use client";

// useComposerResize — composer 顶部手柄拖拽调高（自 ChatStream.tsx 机械拆出，
// 行为逐字一致）。composerH 必须留在父级（composer 在 home/session 两槽按
// key 重挂，状态不能随组件重置）。

import { useRef, useState } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";

export function useComposerResize({
  composerBoxRef,
}: {
  composerBoxRef: MutableRefObject<HTMLDivElement | null>;
}): {
  composerH: number | undefined;
  setComposerH: Dispatch<SetStateAction<number | undefined>>;
  onResizeStart: (e: React.PointerEvent) => void;
} {
  // Composer height: undefined = auto-grow (capped); a number = user-dragged size.
  const [composerH, setComposerH] = useState<number | undefined>(undefined);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  // Drag the composer's top handle to resize the input area. Auto-grow (capped)
  // still applies while composerH is undefined; dragging switches to a fixed,
  // scrollable height. The flex layout (message list = flex-1) keeps the overall
  // experience intact — the list simply takes the remaining space.
  function onResizeStart(e: React.PointerEvent) {
    const box = composerBoxRef.current;
    if (!box) return;
    dragRef.current = { startY: e.clientY, startH: box.offsetHeight };
    const minH = 96;
    const maxH = Math.max(160, Math.floor(window.innerHeight * 0.7));
    const move = (ev: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const h = Math.min(maxH, Math.max(minH, d.startH + (d.startY - ev.clientY)));
      setComposerH(h);
    };
    const up = () => {
      dragRef.current = null;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  return { composerH, setComposerH, onResizeStart };
}
