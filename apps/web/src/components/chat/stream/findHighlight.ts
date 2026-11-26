"use client";

import { FIND_REVEAL_ROW, findOccurrences, type FindHit, type FindTarget } from "@/lib/sessionFind";
import { REVEAL_PROCESS_BLOCK } from "../blocks/ProcessFold";

export const HIGHLIGHT_SUPPORTED =
  typeof CSS !== "undefined" && "highlights" in CSS && typeof globalThis.Highlight !== "undefined";

type OccurrenceTarget = FindTarget & Pick<FindHit, "occurrence">;

function findRow(scrollEl: HTMLElement, rowKey: string): Element | null {
  return scrollEl.querySelector(`[data-find-row="${CSS.escape(rowKey)}"]`);
}

function findBlocks(row: Element, blockIdx: number): Element[] {
  return Array.from(row.querySelectorAll(`[data-block-idx="${blockIdx}"]`));
}

/** Match visible DOM text, allowing a match to span inline Markdown. Each
 * block-level paragraph is a separate run so adjacent labels don't fuse. */
function locateRanges(scope: Element, query: string): Range[] {
  const out: Range[] = [];
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  const separate = "p,div,li,pre,td,th,blockquote,h1,h2,h3,h4,h5,h6,button";
  let parent: Element | null = null;
  let text = "";
  let nodes: Array<{ node: Text; at: number }> = [];
  const appendRanges = () => {
    let start = 0;
    let end = 0;
    for (const match of findOccurrences(text, query)) {
      const from = match.index!;
      const to = from + match[0].length;
      while (start + 1 < nodes.length && nodes[start + 1].at <= from) start++;
      end = Math.max(end, start);
      while (end + 1 < nodes.length && nodes[end + 1].at < to) end++;
      const range = document.createRange();
      range.setStart(nodes[start].node, from - nodes[start].at);
      range.setEnd(nodes[end].node, to - nodes[end].at);
      if (range.getBoundingClientRect().height > 0) out.push(range);
    }
  };
  let node: Text | null;
  while ((node = walker.nextNode() as Text | null)) {
    if (!node.length || node.parentElement?.closest("[hidden], [aria-hidden=true]")) continue;
    const group = node.parentElement?.closest(separate) ?? scope;
    if (group !== parent) {
      appendRanges();
      text = "";
      nodes = [];
      parent = group;
    }
    nodes.push({ node, at: text.length });
    text += node.data;
  }
  appendRanges();
  return out;
}

export function paint(scrollEl: HTMLElement, hits: FindHit[], activeIdx: number, query: string): void {
  if (!HIGHLIGHT_SUPPORTED) return;
  const hitRanges: Range[] = [];
  const activeRanges: Range[] = [];
  const rows = new Map<string, Element | null>();
  const rangesByRow = new Map<Element, Map<number, Range[]>>();
  hits.forEach((hit, index) => {
    if (!rows.has(hit.rowKey)) rows.set(hit.rowKey, findRow(scrollEl, hit.rowKey));
    const row = rows.get(hit.rowKey);
    if (!row) return;
    if (!rangesByRow.has(row)) rangesByRow.set(row, new Map());
    const ranges = rangesByRow.get(row)!;
    if (!ranges.has(hit.blockIdx)) {
      ranges.set(hit.blockIdx, findBlocks(row, hit.blockIdx).flatMap((block) => locateRanges(block, query)));
    }
    const range = ranges.get(hit.blockIdx)?.[hit.occurrence];
    if (range) (index === activeIdx ? activeRanges : hitRanges).push(range);
  });
  CSS.highlights.set("find-hit", new Highlight(...hitRanges));
  CSS.highlights.set("find-hit-active", new Highlight(...activeRanges));
}

export function clear(): void {
  if (!HIGHLIGHT_SUPPORTED) return;
  CSS.highlights.delete("find-hit");
  CSS.highlights.delete("find-hit-active");
}

/** Reusable mounting and reveal path for find targets, including absorbed rows.
 * The caller can retry this after loading an older page or moving the window. */
export function focusActiveHit(
  scrollEl: HTMLElement,
  hit: OccurrenceTarget,
  query: string,
  onSettled: () => void,
): () => void {
  let tries = 0;
  let frame = 0;
  let revealed: Element | undefined;
  const retry = () => {
    if (++tries < 60) frame = requestAnimationFrame(tick);
    else onSettled();
  };
  const tick = () => {
    const row = findRow(scrollEl, hit.rowKey);
    if (!row) return retry();
    const group = row.querySelector(`[data-process-blocks~="${hit.blockIdx}"]`);
    if (group?.querySelector(":scope > button[aria-expanded=false]")) {
      if (revealed !== group) {
        revealed = group;
        group.dispatchEvent(new Event(REVEAL_PROCESS_BLOCK));
      }
      return retry();
    }
    const blocks = findBlocks(row, hit.blockIdx);
    const block = blocks[0];
    if (!block) {
      row.scrollIntoView({ block: "center" });
      onSettled();
      return;
    }
    const record = block.querySelector<HTMLButtonElement>("[data-reveal-record][aria-expanded=false]");
    if (record && revealed !== record) {
      revealed = record;
      record.click();
      return retry();
    }
    if (block.querySelector("[data-tool-detail-loading]")) {
      frame = requestAnimationFrame(tick);
      return;
    }
    const target = blocks.flatMap((part) => locateRanges(part, query))[hit.occurrence];
    const anchor = target?.startContainer.parentElement ?? block;
    anchor.scrollIntoView({ block: "center" });
    // A long text node can hold several matches on different lines. Center
    // the chosen range in each scrolling ancestor, including the fold body.
    if (target) {
      for (let parent: Element | null = anchor; parent; parent = parent.parentElement) {
        if (parent === scrollEl || /^(auto|scroll)$/.test(getComputedStyle(parent).overflowY)) {
          const range = target.getBoundingClientRect();
          const viewport = parent.getBoundingClientRect();
          parent.scrollTop += (range.top + range.bottom - viewport.top - viewport.bottom) / 2;
        }
        if (parent === scrollEl) break;
      }
    }
    onSettled();
  };
  window.dispatchEvent(new CustomEvent(FIND_REVEAL_ROW, { detail: { rowKey: hit.rowKey, scrollEl } }));
  frame = requestAnimationFrame(tick);
  return () => cancelAnimationFrame(frame);
}
