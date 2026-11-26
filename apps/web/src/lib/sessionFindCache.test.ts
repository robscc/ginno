import { describe, expect, it, vi } from "vitest";
import type { getSessionHistoryIndex } from "./api/sessions";
import { createSessionFindCache } from "./sessionFindCache";

type FetchIndex = typeof getSessionHistoryIndex;
type IndexResponse = Awaited<ReturnType<FetchIndex>>;

function index(revision: string, id: string): IndexResponse {
  return { ok: true, revision, messages: [{ id, role: "user", blocks: [] }] };
}

function deferred() {
  let resolve!: (response: IndexResponse) => void;
  const promise = new Promise<IndexResponse>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("session find index cache", () => {
  it("shares a pending check, reuses the snapshot on validation and on page catch-up", async () => {
    const first = deferred();
    const rows = index("new", "first");
    const fetchIndex = vi.fn<FetchIndex>().mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce({ ok: true, revision: "new", unchanged: true });
    const cache = createSessionFindCache(fetchIndex);
    const pending = cache.read("session", "previous", true);
    expect(cache.read("session", "previous", true)).toBe(pending);
    first.resolve(rows);
    expect(await pending).toBe(rows);
    expect(await cache.read("session", "new", false)).toBe(rows);
    expect(await cache.read("session", "new", true)).toBe(rows);
    expect(fetchIndex.mock.calls.map(([sid, revision]) => [sid, revision])).toEqual([
      ["session", undefined], ["session", "new"],
    ]);
  });

  it("isolates equal revisions by session and evicts the least recently used snapshot", async () => {
    const fetchIndex = vi.fn<FetchIndex>().mockImplementation(async (sid, revision) => revision
      ? { ok: true, revision, unchanged: true } : index("same", sid));
    const cache = createSessionFindCache(fetchIndex);
    const one = await cache.read("one", "same", true);
    expect(await cache.read("two", "same", true)).toEqual(index("same", "two"));
    await cache.read("three", "same", true);
    expect(await cache.read("one", "same", false)).toBe(one);
    await cache.read("four", "same", true);
    expect(await cache.read("one", "same", true)).toBe(one);
    expect(await cache.read("two", "same", true)).toEqual(index("same", "two"));
    expect(fetchIndex.mock.calls.map(([sid, revision]) => [sid, revision])).toEqual([
      ["one", undefined], ["two", undefined], ["three", undefined],
      ["four", undefined], ["one", "same"], ["two", undefined],
    ]);
  });

  it.each(["new page revision", "checkpoint event", "eviction"])(
    "discards a late successful response after %s", async (change) => {
      const old = deferred();
      const recent = index("v2", "new row");
      const fetchIndex = vi.fn<FetchIndex>().mockReturnValueOnce(old.promise)
        .mockImplementation(async (sid) => sid === "active" ? recent : index("v1", sid));
      const cache = createSessionFindCache(fetchIndex);
      const stale = cache.read("active", "v1", true);
      const dropped = expect(stale).rejects.toMatchObject({ name: "AbortError" });
      if (change === "checkpoint event") cache.invalidate("active");
      if (change === "eviction") {
        for (const other of ["b", "c", "d"]) await cache.read(other, "v1", true);
      }
      expect(await cache.read("active", change === "new page revision" ? "v2" : "v1", true)).toBe(recent);
      expect(fetchIndex.mock.calls[0][2]?.aborted).toBe(true);
      old.resolve(index("v1", "removed row"));
      await dropped;
      expect(await cache.read("active", "v2", false)).toBe(recent);
      expect(fetchIndex.mock.calls.filter(([sid]) => sid === "active")).toHaveLength(2);
    },
  );

  it("retries transport and invalid responses without caching them", async () => {
    const fresh = index("v2", "new row");
    const fetchIndex = vi.fn<FetchIndex>()
      .mockRejectedValueOnce(new Error("HTTP 503"))
      .mockResolvedValueOnce({ ok: true, revision: "v1", unchanged: true })
      .mockResolvedValueOnce(fresh);
    const cache = createSessionFindCache(fetchIndex);
    await expect(cache.read("session", "v1", true)).rejects.toThrow("503");
    await expect(cache.read("session", "v1", true)).rejects.toThrow("Invalid history index validation");
    expect(await cache.read("session", "v1", true)).toBe(fresh);
    expect(fetchIndex.mock.calls.map(([, revision]) => revision)).toEqual([undefined, undefined, undefined]);
  });
});
