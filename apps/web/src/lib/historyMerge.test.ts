import { describe, expect, it } from "vitest";
import { mergeHistoryWithLive, msgText, type MergeMsg } from "./historyMerge";

const m = (over: Partial<MergeMsg> & { id: string }): MergeMsg => ({
  role: "assistant",
  blocks: [],
  ...over,
});
const text = (id: string, t: string, role: MergeMsg["role"] = "assistant"): MergeMsg =>
  m({ id, role, blocks: [{ kind: "text", text: t }] });

describe("msgText", () => {
  it("joins only text blocks", () => {
    const msg: MergeMsg = {
      id: "x",
      role: "user",
      blocks: [
        { kind: "image", url: "data:…" },
        { kind: "text", text: "hello " },
        { kind: "text", text: "world" },
        { kind: "file", fileId: "f1", name: "a.txt", path: "/a", fileKind: "doc" },
      ],
    };
    expect(msgText(msg)).toBe("hello \nworld");
  });
});

describe("mergeHistoryWithLive", () => {
  it("returns the snapshot untouched when nothing went live", () => {
    const hist = [text("u1", "hi", "user"), text("a1", "hello!")];
    expect(mergeHistoryWithLive(hist, [])).toEqual(hist);
  });

  it("never overwrites live messages (issue #16 core case)", () => {
    // history resolves AFTER a live turn already rendered — live wins.
    const hist = [text("u1", "old question", "user"), text("a1", "old answer")];
    const live = [text("pm1", "new question", "user"), text("pm2", "new answer…")];
    const merged = mergeHistoryWithLive(hist, live);
    expect(merged).toEqual([...hist, ...live]);
    expect(merged.find((x) => x.id === "pm2")?.blocks[0]).toMatchObject({ text: "new answer…" });
  });

  it("drops the history user+assistant pair already rendered live (mid-fetch turn)", () => {
    const hist = [
      text("u1", "earlier question", "user"),
      text("a1", "earlier answer"),
      text("u2", "fast question", "user"),
      text("a2", "fast answer"),
    ];
    const live = [text("pm1", "fast question", "user"), text("pm2", "fast answer")];
    const merged = mergeHistoryWithLive(hist, live);
    expect(merged.map((x) => x.id)).toEqual(["u1", "a1", "pm1", "pm2"]);
  });

  it("keeps the history assistant when the paired live turn has none yet", () => {
    // user turn started live but no assistant bubble content exists yet —
    // the snapshot's answer is kept (prefacing the live user bubble; window
    // is one tick, resolved before turn.start rendered an assistant bubble).
    const hist = [text("u1", "q", "user"), text("a1", "full answer")];
    const live = [text("pm1", "q", "user")];
    expect(mergeHistoryWithLive(hist, live).map((x) => x.id)).toEqual(["a1", "pm1"]);
  });

  it("dedupes by id collision with live ids/turnIds", () => {
    const hist = [m({ id: "srv1", role: "user", blocks: [{ kind: "text", text: "qq" }] })];
    const live = [m({ id: "pm1", role: "user", turnId: "srv1", blocks: [{ kind: "text", text: "other" }] })];
    expect(mergeHistoryWithLive(hist, live).map((x) => x.id)).toEqual(["pm1"]);
  });

  it("drops a duplicate live error card but keeps a novel one", () => {
    const hist = [
      text("u1", "q", "user"),
      { ...text("err1", "boom"), error: true },
      { ...text("err2", "other failure"), error: true },
    ];
    const live = [text("pm1", "q", "user"), { ...text("pmE", "boom"), error: true }];
    const merged = mergeHistoryWithLive(hist, live);
    expect(merged.map((x) => x.id)).toEqual(["err2", "pm1", "pmE"]);
  });

  it("keeps system rows from both sides", () => {
    const hist = [m({ id: "s0", role: "system", blocks: [{ kind: "context", text: "ctx" }] })];
    const live = [m({ id: "s1", role: "system", blocks: [{ kind: "context", text: "live ctx" }] })];
    expect(mergeHistoryWithLive(hist, live).map((x) => x.id)).toEqual(["s0", "s1"]);
  });

  it("tolerates tool/steer blocks in live bubbles (token/tool/steer mix)", () => {
    const live: MergeMsg[] = [
      text("pm1", "run the tool", "user"),
      m({
        id: "pm2",
        role: "assistant",
        blocks: [
          { kind: "text", text: "checking…" },
          { kind: "tool", id: "t1", name: "bash", content: "…", pending: true },
          { kind: "steer", text: "also do x", steerId: "st1", injectedAt: 1 },
        ],
      }),
    ];
    const hist = [text("uOld", "previous", "user"), text("aOld", "done")];
    const merged = mergeHistoryWithLive(hist, live);
    expect(merged.slice(0, 2).map((x) => x.id)).toEqual(["uOld", "aOld"]);
    expect(merged[2]).toBe(live[0]);
    expect(merged[3].blocks).toHaveLength(3); // live bubble passed through intact
  });
});
