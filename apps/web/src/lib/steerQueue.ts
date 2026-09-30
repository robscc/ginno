/**
 * Mid-turn steering queue — shared by the main chat (ChatStream.tsx) and the
 * floating quick-chat window (PinStream.tsx).
 *
 * Moved verbatim out of ChatStream.tsx (steer-queue-shared-brief §3.1). The
 * hook owns ONLY "what is queued, how it is sent, how it is acked, how it is
 * recalled". Presentation (the queue bar / steer band), history replay and the
 * draft cache stay with each caller — that split is the difference between the
 * two windows.
 *
 * ⚠️ The ack timing here is load-bearing and only a real browser exposed its
 * bug (docs/steering-design.md「实现期推翻的一处设计假设」): the server acks at
 * DRAIN time, not at superstep commit. Do not "optimise" the order of the ref
 * writes or the send calls — a wrong order re-introduces the band-after-续写 bug.
 */
import { useCallback, useRef, useState } from "react";
import type { ComposerImage, ComposerFile } from "@/lib/composerAttachments";

/** One queued mid-turn injection. Mirrors the frame the server expects. */
export interface SteerItem {
  steerId: string;
  text: string;
  /** The turn this rides ("" = let the server use its own running-turn id). */
  turnId: string;
  /** Captured at enqueue: the agent the message was composed against, so a
   *  later re-send-as-invoke keeps the user's intent. */
  agentId: string | null;
  /** "sending" until the server accepts it into the stash, then "queued". */
  status: "sending" | "queued" | "absorbed";
  /** Attachments travel WITH the entry: they ride the steer frame, survive
   *  every re-send, and are handed back to the composer on recall — otherwise
   *  the user silently loses them. */
  images: ComposerImage[];
  files: ComposerFile[];
}

/** Opaque id for a queued entry (never reopened from history — the server
 *  echoes it back on the acks). Local counter, same shape ChatStream used. */
let _steerSeq = 0;
const newSteerId = () => `m${++_steerSeq}`;

export function useSteerQueue() {
  // Per-session queue. A ref, like every other socket-fed store: the socket
  // callbacks outlive session switches and must not read stale React state.
  const queueRef = useRef<Record<string, SteerItem[]>>({});
  // Bumped on every queue mutation so `itemsFor` is re-read on the next render
  // (this replaces ChatStream's setSteerQueue mirror inside syncDisplay).
  const [, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);

  // Write the queue AND mirror it into the caller's render — the same
  // ref-then-notify order as ChatStream's setSteerQueueFor.
  const setFor = useCallback(
    (sid: string, next: SteerItem[]) => {
      if (next.length) queueRef.current[sid] = next;
      else delete queueRef.current[sid];
      bump();
    },
    [bump],
  );

  // Current queue for a session, oldest first, for use during render.
  const itemsFor = useCallback((sid: string): SteerItem[] => queueRef.current[sid] ?? [], []);

  /** Send one entry's steer frame. `send` is the caller's raw-frame sender
   *  (each window has its own socket); the frame shape is fixed (brief §3.3). */
  const sendSteer = useCallback(
    (sid: string, item: SteerItem, send: (frame: unknown) => void) => {
      try {
        send({
          type: "steer",
          steer_id: item.steerId,
          turn_id: item.turnId,
          message: item.text,
          // Only present when non-empty, so a text-only steer stays byte-identical.
          ...(item.images.length
            ? { images: item.images.map((a) => ({ data: a.data, media_type: a.mediaType })) }
            : {}),
          ...(item.files.length
            ? { files: item.files.map((f) => ({ id: f.id, name: f.name, path: f.path })) }
            : {}),
        });
      } catch {
        // Socket gone: the entry stays queued and rides the next turn instead
        // (the caller's flush on message.end), so nothing the user typed is lost.
      }
    },
    [],
  );

  /** Queue a message for absorption by the running turn. It is NOT a turn: the
   *  entry lives in the queue until the server acks it as absorbed. */
  const enqueue = useCallback(
    (args: {
      sessionId: string;
      turnId: string;
      text: string;
      images?: ComposerImage[];
      files?: ComposerFile[];
      agentId?: string | null;
      send: (frame: unknown) => void;
    }) => {
      const item: SteerItem = {
        steerId: newSteerId(),
        text: args.text,
        turnId: args.turnId,
        agentId: args.agentId ?? null,
        status: "sending",
        images: args.images ?? [],
        files: args.files ?? [],
      };
      setFor(args.sessionId, [...(queueRef.current[args.sessionId] ?? []), item]);
      sendSteer(args.sessionId, item, args.send);
    },
    [setFor, sendSteer],
  );

  /** Re-send every queued entry as `steer` — done right before resuming a
   *  parked turn / after a socket reconnect, so an entry stashed into a dropped
   *  socket is not lost. Safe to repeat: server-side enqueue replaces by
   *  steer_id. Idempotent — already-absorbed entries are gone from the queue. */
  const onResume = useCallback(
    (sid: string, send: (frame: unknown) => void) => {
      for (const item of queueRef.current[sid] ?? []) sendSteer(sid, item, send);
    },
    [sendSteer],
  );

  /** ⏹ / ↑ recall: take the session's still-queued entries OUT of the queue and
   *  hand them back to the caller (which restores text + attachments). */
  const recall = useCallback(
    (sid: string): SteerItem[] => {
      const items = queueRef.current[sid] ?? [];
      if (!items.length) return [];
      setFor(sid, []);
      return items;
    },
    [setFor],
  );

  /** Feed one socket event. Returns true when it belongs to the queue (the
   *  caller need not process it further for queue purposes; it may still do its
   *  own display work, e.g. append the absorbed band). */
  const handleEvent = useCallback(
    (ev: { event: string; [k: string]: unknown }, sid: string): boolean => {
      switch (ev.event) {
        case "steer.accepted": {
          // The server stashed the entry: flip it out of "sending". It stays in
          // the queue until it is absorbed.
          if (ev.steer_id) {
            const items = queueRef.current[sid] ?? [];
            if (items.some((i) => i.steerId === ev.steer_id)) {
              setFor(
                sid,
                items.map((i) =>
                  i.steerId === ev.steer_id ? { ...i, status: "queued" as const } : i,
                ),
              );
            }
          }
          return true;
        }
        case "steer.absorbed": {
          // The steered message committed (drained) into state: move it out of
          // the queue. The caller renders the band at the injection point.
          const absorbedId = ev.steer_id as string | undefined;
          if (!absorbedId) return true;
          const items = queueRef.current[sid] ?? [];
          if (!items.some((i) => i.steerId === absorbedId)) return true; // already dropped by a history reconcile
          setFor(sid, items.filter((i) => i.steerId !== absorbedId));
          return true;
        }
        default:
          return false;
      }
    },
    [setFor],
  );

  /** Drop specific ids (the queue-bar ✕, and the history reconcile's absorbed
   *  sweep — a steer_id present in history was absorbed even if the ack was
   *  lost). Exactly-once delivery depends on this. */
  const remove = useCallback(
    (sid: string, steerIds: Iterable<string>) => {
      const items = queueRef.current[sid] ?? [];
      if (!items.length) return;
      const drop = new Set(steerIds);
      const next = items.filter((i) => !drop.has(i.steerId));
      if (next.length !== items.length) setFor(sid, next);
    },
    [setFor],
  );

  /** Pop the oldest entry — the turn ended with entries still unacknowledged,
   *  so the caller promotes it to the next turn (design §3.3, Claude Code's
   *  "only the oldest becomes the next turn" rule). */
  const takeOldest = useCallback(
    (sid: string): SteerItem | null => {
      const items = queueRef.current[sid] ?? [];
      if (!items.length) return null;
      const [head, ...rest] = items;
      setFor(sid, rest);
      return head;
    },
    [setFor],
  );

  /** Drop a session's queue entirely (session deleted / unmounted). */
  const clear = useCallback(
    (sid: string) => {
      delete queueRef.current[sid];
      bump();
    },
    [bump],
  );

  return { itemsFor, enqueue, recall, onResume, handleEvent, remove, takeOldest, clear };
}