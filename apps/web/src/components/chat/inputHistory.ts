/**
 * Composer input history — pure logic (no React), shell-style ↑/↓ recall.
 *
 * 每个输入框(ChatStream 主 composer / PinStream 悬浮窗)各持一份
 * HistoryState;种子取自该会话已加载的用户消息,窗口内新发送的即时 append。
 * 触发条件由调用方判定(光标在首行 ↑ / 末行 ↓),本模块只管状态机与文本。
 *
 * 语义:编辑写回 —— 召回一条后修改,再按 ↑/↓ 时修改保留在该槽位
 * (比 bash 的「导航即丢弃编辑」更符合桌面聊天 App 预期);发送后无条件
 * 回草稿位;连续重复不重复记(同 shell HISTCONTROL=ignoredups)。
 */

import type { Block } from "./blocks";

export interface HistoryState {
  /** oldest → newest;连续重复去重。 */
  entries: string[];
  /** 当前停在第几条;entries.length = 草稿位(最新)。 */
  index: number;
  /** 首次 ↑ 时保存的未发送草稿;↓ 回到草稿位时还原。 */
  draft: string;
}

export function initHistory(entries: string[] = []): HistoryState {
  return { entries, index: entries.length, draft: "" };
}

/** 发送成功后追加并回草稿位。空白跳过;与上一条相同则不重复记。 */
export function appendEntry(s: HistoryState, text: string): HistoryState {
  const t = text.trim();
  if (t && s.entries[s.entries.length - 1] !== t) {
    return { entries: [...s.entries, t], index: s.entries.length + 1, draft: "" };
  }
  return { entries: s.entries, index: s.entries.length, draft: "" };
}

/** 离开当前槽位前的写回:召回后被编辑的文本保留在原槽位(仅中间槽位,
 *  草稿位由 draft 承载)。返回可能更新了 entries 的 state。 */
function writeBack(s: HistoryState, currentText: string): HistoryState {
  if (
    s.index < s.entries.length &&
    currentText.trim() &&
    currentText !== s.entries[s.index]
  ) {
    const entries = s.entries.slice();
    entries[s.index] = currentText;
    return { ...s, entries };
  }
  return s;
}

/** ↑:回溯上一条。currentText 是输入框当前内容。null = 已到头,调用方不动。 */
export function navUp(
  s: HistoryState,
  currentText: string,
): { state: HistoryState; text: string } | null {
  if (s.index <= 0 || s.entries.length === 0) return null;
  let st = writeBack(s, currentText);
  if (st.index === st.entries.length) st = { ...st, draft: currentText };
  const index = st.index - 1;
  return { state: { ...st, index }, text: st.entries[index] ?? "" };
}

/** ↓:回到下一条;到草稿位时还原 draft。null = 已在草稿位。 */
export function navDown(
  s: HistoryState,
  currentText: string,
): { state: HistoryState; text: string } | null {
  if (s.index >= s.entries.length) return null;
  const st = writeBack(s, currentText);
  const index = st.index + 1;
  const text = index === st.entries.length ? st.draft : (st.entries[index] ?? "");
  return { state: { ...st, index }, text };
}

/** 种子构建:trim → 滤空 → 连续去重。steer-only / subagent 注入 / 纯附件
 *  消息在来源侧(payloadFromBlocks / userTextFromBlocks)text 已为空,在此滤除。 */
export function seedEntries(userTexts: string[]): string[] {
  const out: string[] = [];
  for (const raw of userTexts) {
    const t = raw.trim();
    if (t && out[out.length - 1] !== t) out.push(t);
  }
  return out;
}

/** 从 user 消息 blocks 提取原始输入文本(text 块直取、skill 块还原
 *  `/name request`),语义镜像 streamCore 的 payloadFromBlocks——但只取文本。
 *  PinStream 的种子来源(它的消息不带 sendPayload)。 */
export function userTextFromBlocks(blocks: Block[]): string {
  const parts: string[] = [];
  for (const b of blocks) {
    if (b.kind === "text") parts.push(b.text);
    else if (b.kind === "skill") parts.push(b.text ? `/${b.name} ${b.text}` : `/${b.name}`);
  }
  return parts.join("\n").trim();
}

/** 光标前无换行 = 在首行(caret 是 textarea 的 selectionStart 字符偏移)。 */
export function caretOnFirstLine(text: string, caret: number): boolean {
  return !text.slice(0, caret).includes("\n");
}

/** 光标后无换行 = 在末行。 */
export function caretOnLastLine(text: string, caret: number): boolean {
  return !text.slice(caret).includes("\n");
}
