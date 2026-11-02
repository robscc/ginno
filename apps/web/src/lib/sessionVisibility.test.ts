import { describe, expect, it } from "vitest";
import { hiddenSessionIds } from "./sessionVisibility";
import type { SessionMeta } from "@/lib/types";

// Minimal SessionMeta: hiddenSessionIds only reads id / type / parent_session_id.
function meta(partial: Partial<SessionMeta> & { id: string }): SessionMeta {
  return { ...partial } as unknown as SessionMeta;
}

describe("hiddenSessionIds", () => {
  it("hides nothing for plain top-level sessions", () => {
    const ids = hiddenSessionIds([meta({ id: "a" }), meta({ id: "b" })]);
    expect(ids.size).toBe(0);
  });

  it("hides scheduled / team / team-member sessions", () => {
    const ids = hiddenSessionIds([
      meta({ id: "s1", type: "scheduled" }),
      meta({ id: "t1", type: "team" }),
      meta({ id: "t2", type: "team-member" }),
      meta({ id: "ok" }),
    ]);
    expect(ids).toEqual(new Set(["s1", "t1", "t2"]));
  });

  it("hides children of hidden sessions (transitively)", () => {
    const ids = hiddenSessionIds([
      meta({ id: "sched", type: "scheduled" }),
      meta({ id: "sub1", parent_session_id: "sched" }),
      meta({ id: "sub2", parent_session_id: "sub1" }), // depth 2
      meta({ id: "deep", parent_session_id: "sub2" }), // depth 3
    ]);
    expect(ids).toEqual(new Set(["sched", "sub1", "sub2", "deep"]));
  });

  it("is order-independent: child listed before its hidden parent", () => {
    const ids = hiddenSessionIds([
      meta({ id: "child", parent_session_id: "shadow" }),
      meta({ id: "shadow", type: "team" }),
    ]);
    expect(ids).toEqual(new Set(["shadow", "child"]));
  });

  it("keeps a visible parent's children visible", () => {
    const ids = hiddenSessionIds([
      meta({ id: "main" }),
      meta({ id: "main-sub", parent_session_id: "main" }),
    ]);
    expect(ids.size).toBe(0);
  });

  it("handles several hidden roots at once", () => {
    const ids = hiddenSessionIds([
      meta({ id: "s1", type: "scheduled" }),
      meta({ id: "s1-child", parent_session_id: "s1" }),
      meta({ id: "t1", type: "team" }),
      meta({ id: "t1-child", parent_session_id: "t1" }),
    ]);
    expect(ids).toEqual(new Set(["s1", "s1-child", "t1", "t1-child"]));
  });
});
