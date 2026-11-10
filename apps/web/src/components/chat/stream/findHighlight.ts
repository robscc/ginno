"use client";

/**
 * findHighlight — CSS Custom Highlight API painter（find-bar-design §5.2-§5.4）。
 *
 * 单路径：能力缺失（macOS 13 / 老 Safari）时 paint 静默 no-op，功能退化
 * 为「计数 + 遍历 + 消息级滚动 + violet ring」（§5.3，不做第二套 <mark>）。
 * 定位用 DOM 二次匹配（blockIdx → data-block-idx 容器 → TreeWalker），
 * 与索引 start 偏移完全独立（§5.4）。
 */

import type { FindHit } from "@/lib/sessionFind";

export const HIGHLIGHT_SUPPORTED =
  typeof CSS !== "undefined" &&
  "highlights" in CSS &&
  typeof (globalThis as { Highlight?: unknown }).Highlight !== "undefined";

function cssEscape(s: string): string {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

/** 在 block 容器子树内做不区分大小写的二次匹配，返回 Range 列表。 */
function locateRanges(scope: Element, query: string): Range[] {
  const out: Range[] = [];
  const needle = query.toLowerCase();
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  const ranges: Array<{ node: Text; start: number }> = [];
  let node: Node | null;
  while ((node = walker.nextNode()) !== null) {
    const text = (node as Text).data.toLowerCase();
    let from = 0;
    for (;;) {
      const at = text.indexOf(needle, from);
      if (at < 0) break;
      ranges.push({ node: node as Text, start: at });
      from = at + Math.max(1, needle.length);
    }
  }
  for (const r of ranges) {
    const range = document.createRange();
    range.setStart(r.node, r.start);
    range.setEnd(r.node, r.start + query.length);
    out.push(range);
  }
  return out;
}

export function paint(
  scrollEl: HTMLElement,
  hits: FindHit[],
  activeIdx: number,
  query: string,
): void {
  if (!HIGHLIGHT_SUPPORTED) return; // §5.3 降级：painter no-op
  const HighlightCtor = (globalThis as { Highlight: new (...r: Range[]) => unknown }).Highlight;
  const hitRanges: Range[] = [];
  const activeRanges: Range[] = [];
  const needle = query.trim();
  if (needle) {
    hits.forEach((h, i) => {
      const row = scrollEl.querySelector(`[data-msg-id="${cssEscape(h.msgId)}"]`);
      if (!row) return; // 窗口外：数据层计数仍含它（§4.1）
      // block 级容器（data-block-idx，§5.4）；无该属性的行整行匹配（P0 容错）。
      const blockEl = row.querySelector(`[data-block-idx="${h.blockIdx}"]`) ?? row;
      const ranges = locateRanges(blockEl, needle);
      (i === activeIdx ? activeRanges : hitRanges).push(...ranges);
    });
  }
  const hs = CSS.highlights as unknown as Map<string, unknown>;
  hs.set("find-hit", new HighlightCtor(...hitRanges));
  hs.set("find-hit-active", new HighlightCtor(...activeRanges));
}

export function clear(): void {
  if (!HIGHLIGHT_SUPPORTED) return;
  const hs = CSS.highlights as unknown as Map<string, unknown>;
  hs.delete("find-hit");
  hs.delete("find-hit-active");
}

/**
 * 跳转定位（§4.2 第 3 步）：dispatch 消息级事件后，等目标行渲染完成再
 * paint + 把当前项滚到视口 1/3。≤60 帧重试（照抄 ⌘K 锚点监听模式）。
 */
export function focusActiveHit(
  scrollEl: HTMLElement,
  hit: FindHit,
  query: string,
  onSettled?: () => void,
): void {
  const needle = query.trim();
  let tries = 0;
  const tick = () => {
    const row = scrollEl.querySelector(`[data-msg-id="${cssEscape(hit.msgId)}"]`);
    if (row) {
      const blockEl = row.querySelector(`[data-block-idx="${hit.blockIdx}"]`) ?? row;
      if (needle && HIGHLIGHT_SUPPORTED) {
        const ranges = locateRanges(blockEl, needle);
        if (ranges[0]) {
          ranges[0].startContainer.parentElement?.scrollIntoView({ block: "center" });
        }
      }
      onSettled?.();
      return;
    }
    if (++tries < 60) requestAnimationFrame(tick);
    else onSettled?.();
  };
  tick();
}
