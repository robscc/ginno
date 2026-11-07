/**
 * 通知总线未读/已读通道（notice-unification-design.md §3）纯逻辑测试。
 * node 环境：localStorage 用内存 stub，rAF 在 lib 内已守卫为同步执行。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  NOTICEBOX_KEY,
  clearNotices,
  hydrateNoticebox,
  markAllNoticesRead,
  markNoticeRead,
  markNoticeReadByKey,
  pushNotice,
  subscribeInbox,
  subscribeNotices,
  subscribeUnreadCount,
  unreadNoticeCount,
  type InboxEntry,
} from "./notice";

// ---- 内存 localStorage stub（vitest node 环境没有 DOM storage；类型来自
// tsconfig 的 dom lib，运行时赋值到 globalThis 即可）----
class MemoryStorage {
  private map = new Map<string, string>();
  getItem(k: string) {
    return this.map.has(k) ? this.map.get(k)! : null;
  }
  setItem(k: string, v: string) {
    this.map.set(k, String(v));
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
  clear() {
    this.map.clear();
  }
}

beforeEach(() => {
  (globalThis as { localStorage?: Storage }).localStorage = new MemoryStorage() as unknown as Storage;
  clearNotices();
});

afterEach(() => {
  vi.useRealTimers();
});

function lastInbox(): InboxEntry[] {
  let out: InboxEntry[] = [];
  const un = subscribeInbox((e) => {
    out = e;
  });
  un(); // 订阅即回放快照，取完即注销
  return out;
}

function toastCount(): number {
  let out = -1;
  const un = subscribeNotices((l) => {
    out = l.length;
  });
  un();
  return out;
}

describe("mode 通道（§3.1）", () => {
  it("silent 不进 toast 队列但进收件箱", () => {
    let toasts = -1;
    const un = subscribeNotices((l) => {
      toasts = l.length;
    });
    pushNotice("info", "后台完成", { mode: "silent" });
    expect(toasts).toBe(0); // 订阅即回放：silent push 前后 toast 队列都是 0
    un();
    const box = lastInbox();
    expect(box).toHaveLength(1);
    expect(box[0].text).toBe("后台完成");
    expect(box[0].mode).toBe("silent");
  });

  it("toast（默认）同时进 toast 队列与收件箱", () => {
    let toasts = 0;
    const un = subscribeNotices((l) => {
      toasts = l.length;
    });
    pushNotice("warn", "普通提示");
    expect(toasts).toBe(1);
    un();
    expect(lastInbox()).toHaveLength(1);
  });
});

describe("已读状态机四种触发（§3.2）", () => {
  it("触发①：markNoticeRead（模拟点 action/×）→ 未读清零，幂等", () => {
    pushNotice("info", "a");
    pushNotice("info", "b");
    expect(unreadNoticeCount()).toBe(2);
    const id = lastInbox()[0].id;
    markNoticeRead(id);
    expect(unreadNoticeCount()).toBe(1);
    markNoticeRead(id); // 幂等：重复点不报错不重复计
    expect(unreadNoticeCount()).toBe(1);
  });

  it("触发②：toast 超时自动消失不算已读（保留收件箱未读态）", () => {
    vi.useFakeTimers();
    pushNotice("info", "会超时");
    expect(unreadNoticeCount()).toBe(1);
    vi.advanceTimersByTime(6200); // 默认 6000ms + 退场 180ms
    expect(toastCount()).toBe(0); // 卡片已消失
    expect(unreadNoticeCount()).toBe(1); // 但未读
    expect(lastInbox()[0].readAt).toBeNull();
  });

  it("触发③：markNoticeReadByKey（收件箱点 session 条目 / 双写路径）", () => {
    pushNotice("info", "s1", { dedupeKey: "session:s1", kind: "session", countInBadge: false, mode: "silent" });
    pushNotice("info", "普通", { mode: "silent" });
    markNoticeReadByKey("session:s1");
    expect(unreadNoticeCount()).toBe(1); // 只清投影，普通条目仍在
    expect(lastInbox().find((e) => e.dedupeKey === "session:s1")!.readAt).not.toBeNull();
  });

  it("触发④：markAllNoticesRead 全清", () => {
    pushNotice("info", "a");
    pushNotice("warn", "b");
    pushNotice("error", "c");
    markAllNoticesRead();
    expect(unreadNoticeCount()).toBe(0);
    expect(lastInbox().every((e) => e.readAt !== null)).toBe(true);
  });
});

describe("badge 口径（§3.3 防双算）", () => {
  it("countInBadge:false 的 session 投影不计入未读数", () => {
    pushNotice("info", "s", { mode: "silent", kind: "session", countInBadge: false });
    expect(unreadNoticeCount()).toBe(0);
    pushNotice("info", "s2", { mode: "silent", kind: "session" }); // 未显式关
    expect(unreadNoticeCount()).toBe(1); // 默认 true
  });

  it("subscribeUnreadCount 推送计数变化", () => {
    const seen: number[] = [];
    const un = subscribeUnreadCount((n) => seen.push(n));
    pushNotice("info", "a");
    pushNotice("info", "b", { countInBadge: false });
    markAllNoticesRead();
    un();
    expect(seen.at(-1)).toBe(0);
    expect(seen).toContain(1);
  });
});

describe("持久化（§3.4）", () => {
  it("round-trip：push → localStorage → hydrate 恢复（含已读态与投影字段）", () => {
    pushNotice("info", "保留1");
    pushNotice("error", "已读的那条");
    pushNotice("info", "会话投影", {
      mode: "silent",
      kind: "session",
      countInBadge: false,
      dedupeKey: "session:x",
      actionMeta: { kind: "session", id: "x" },
    });
    markNoticeRead(lastInbox().find((e) => e.text === "已读的那条")!.id);
    const before = lastInbox();
    expect(before).toHaveLength(3);

    // 模拟重载：hydrate 从 localStorage 恢复
    const restored = hydrateNoticebox();
    expect(restored).toHaveLength(3);
    const readOne = restored.find((e) => e.text === "已读的那条")!;
    expect(readOne.readAt).not.toBeNull();
    const proj = restored.find((e) => e.dedupeKey === "session:x")!;
    expect(proj.countInBadge).toBe(false);
    expect(proj.actionMeta).toEqual({ kind: "session", id: "x" });
    // 恢复后新 push 的 id 不与恢复条目冲突
    pushNotice("info", "重启后新通知");
    expect(lastInbox().some((e) => e.text === "重启后新通知")).toBe(true);
  });

  it("损坏数据静默重置为空（不抛异常）", () => {
    globalThis.localStorage.setItem(NOTICEBOX_KEY, "{not valid json");
    expect(() => hydrateNoticebox()).not.toThrow();
    expect(unreadNoticeCount()).toBe(0);
    expect(lastInbox()).toHaveLength(0);
  });

  it("非数组载荷同样静默重置", () => {
    globalThis.localStorage.setItem(NOTICEBOX_KEY, JSON.stringify({ oops: true }));
    hydrateNoticebox();
    expect(lastInbox()).toHaveLength(0);
  });
});

describe("dedupeKey（刷新不堆叠）", () => {
  it("同 key 重复 push 收件箱只有一条，且刷新重置未读", () => {
    pushNotice("info", "第一次", { dedupeKey: "session:s1", mode: "silent" });
    markNoticeReadByKey("session:s1");
    expect(unreadNoticeCount()).toBe(0);
    pushNotice("info", "第二次", { dedupeKey: "session:s1", mode: "silent" });
    const box = lastInbox();
    expect(box.filter((e) => e.dedupeKey === "session:s1")).toHaveLength(1);
    expect(box.find((e) => e.dedupeKey === "session:s1")!.text).toBe("第二次");
    expect(box.find((e) => e.dedupeKey === "session:s1")!.readAt).toBeNull(); // 重新未读
  });

  it("滚动窗口：超过 100 条裁掉最旧", () => {
    for (let i = 0; i < 105; i++) pushNotice("info", `n${i}`, { mode: "silent" });
    const box = lastInbox();
    expect(box).toHaveLength(100);
    expect(box[0].text).toBe("n104"); // 最新在前
    expect(box.some((e) => e.text === "n0")).toBe(false);
  });
});

describe("clearNotices（登出/测试）", () => {
  it("toast 队列 + 收件箱 + 持久化全清", () => {
    pushNotice("info", "x");
    clearNotices();
    expect(unreadNoticeCount()).toBe(0);
    expect(globalThis.localStorage.getItem(NOTICEBOX_KEY)).toBe("[]");
  });
});
