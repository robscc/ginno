import { getSessionHistoryIndex, type HistoryIndex } from "./api/sessions";

const MAX_SESSIONS = 3;

interface Read {
  pageRevision: string | undefined;
  controller: AbortController;
  promise: Promise<HistoryIndex>;
}

interface Entry {
  snapshot?: HistoryIndex;
  pending?: Read;
  needsValidation?: boolean;
}

export function createSessionFindCache(fetchIndex = getSessionHistoryIndex) {
  // One full snapshot and one pending validation per session, with LRU eviction.
  const entries = new Map<string, Entry>();
  function cancel(entry: Entry) {
    entry.pending?.controller.abort();
    entry.pending = undefined;
  }
  return {
    invalidate(sid: string) {
      const entry = entries.get(sid);
      if (!entry) return;
      cancel(entry);
      entry.needsValidation = true;
    },
    read(sid: string, pageRevision: string | undefined, verify: boolean): Promise<HistoryIndex> {
      const entry = entries.get(sid) ?? {};
      entries.delete(sid);
      entries.set(sid, entry);
      if (entries.size > MAX_SESSIONS) {
        const oldest = entries.keys().next().value!;
        cancel(entries.get(oldest)!);
        entries.delete(oldest);
      }
      const { pending, snapshot: prior } = entry;
      if (pending && pending.pageRevision === pageRevision) return pending.promise;
      if (!verify && !entry.needsValidation && !pending && prior && prior.revision === pageRevision) {
        return Promise.resolve(prior);
      }
      cancel(entry);
      const controller = new AbortController();
      const promise = fetchIndex(sid, prior?.revision, controller.signal).then((result) => {
        if (entry.pending?.controller !== controller) throw new DOMException("Index read superseded", "AbortError");
        if (result?.ok !== true || typeof result.revision !== "string") throw new Error("Invalid history index");
        if ("unchanged" in result) {
          if (result.unchanged !== true || !prior || result.revision !== prior.revision) {
            throw new Error("Invalid history index validation");
          }
          entry.needsValidation = false;
          return prior;
        }
        if (!Array.isArray(result.messages) || !result.messages.every((row) =>
          row && typeof row.id === "string" && ["user", "assistant", "system"].includes(row.role) &&
          Array.isArray(row.blocks) && row.blocks.every((block) => block && typeof block.kind === "string"))) {
          throw new Error("Invalid history index rows");
        }
        entry.snapshot = result;
        entry.needsValidation = false;
        return result;
      }).finally(() => {
        if (entry.pending?.controller === controller) entry.pending = undefined;
      });
      entry.pending = { pageRevision, controller, promise };
      return promise;
    },
  };
}

export const sessionFindCache = createSessionFindCache();
export const FIND_HISTORY_CHANGED = "ginno:find-history-changed";
