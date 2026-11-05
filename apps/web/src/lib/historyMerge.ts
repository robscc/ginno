/**
 * PinStream history/realtime merge (issue #16).
 *
 * `getSessionHistory()` resolves AFTER the WS `connect()` — a plain
 * `setMessages(mapHistory(res))` would clobber any live frames that landed
 * while the fetch was in flight (fast turns, steering, error cards). This
 * module holds the pure merge strategy so it can be unit-tested without
 * React: the snapshot only FILLS what precedes the live segment, never
 * overwrites it.
 *
 * Identity rules (server ids don't align with client bubble ids):
 * - a history message whose `id` collides with any live `id`/`turnId` is a
 *   duplicate → dropped;
 * - a history USER message whose flattened text equals a live user bubble's
 *   is the same turn (the server persisted it mid-fetch) → dropped, and the
 *   history ASSISTANT bubble that directly follows it is dropped too (the
 *   live segment already renders that turn);
 * - a history error card whose text matches a live error card is dropped;
 * - everything else is kept, history first, live after (live is newer).
 */

import type { Block } from "@/components/chat/blocks";

export interface MergeMsg {
  id: string;
  role: "user" | "assistant" | "system";
  blocks: Block[];
  turnId?: string;
  error?: boolean;
  retryText?: string;
}

/** Flattened text of a message's text blocks (dedupe + error-card key). */
export function msgText(m: MergeMsg): string {
  return m.blocks
    .filter((b) => b.kind === "text")
    .map((b) => (typeof b.text === "string" ? b.text : ""))
    .join("\n")
    .trim();
}

export function mergeHistoryWithLive(history: MergeMsg[], live: MergeMsg[]): MergeMsg[] {
  // live user text → does an assistant bubble follow it in the live segment?
  const liveUserTurns = new Map<string, boolean>();
  for (let i = 0; i < live.length; i++) {
    const mu = live[i];
    if (mu.role !== "user") continue;
    const t = msgText(mu);
    if (!t) continue;
    let hasAssistant = false;
    for (let j = i + 1; j < live.length && live[j].role !== "user"; j++) {
      if (live[j].role === "assistant") hasAssistant = true;
    }
    liveUserTurns.set(t, hasAssistant);
  }
  const liveIds = new Set<string>();
  for (const m of live) {
    if (m.id) liveIds.add(m.id);
    if (m.turnId) liveIds.add(m.turnId);
  }
  const liveErrorTexts = new Set(
    live.filter((m) => m.error).map(msgText).filter(Boolean),
  );

  const out: MergeMsg[] = [];
  // The history assistant bubble directly following a dropped user message
  // belongs to the same (already-live-rendered) turn → skip exactly one.
  let skipNextAssistant = false;
  for (const h of history) {
    if (h.id && liveIds.has(h.id)) {
      skipNextAssistant = h.role === "user";
      continue;
    }
    if (h.role === "user") {
      const t = msgText(h);
      const paired = t ? liveUserTurns.get(t) : undefined;
      if (paired !== undefined) {
        // The user turn went live mid-fetch: drop the snapshot copy; skip the
        // snapshot's assistant bubble only when the live segment already
        // renders one (otherwise keep it — the live bubble may not exist yet).
        skipNextAssistant = paired;
        continue;
      }
      skipNextAssistant = false;
      out.push(h);
      continue;
    }
    if (h.role === "assistant") {
      // Error cards (role "assistant" + error) may already be rendered live —
      // dedupe by text; they never consume the turn-pairing skip below.
      if (h.error) {
        const t = msgText(h);
        if (!(t && liveErrorTexts.has(t))) out.push(h);
        continue;
      }
      if (skipNextAssistant) {
        skipNextAssistant = false;
        continue;
      }
      out.push(h);
      continue;
    }
    // system rows: keep as-is.
    out.push(h);
  }
  return [...out, ...live];
}
